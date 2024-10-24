# Copyright (c) 2022-2024, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# See LICENSE for license information.

"""Fused Adam optimizer."""
import torch
import transformer_engine_torch as tex
from transformer_engine.pytorch.float8_tensor import Float8Tensor
from transformer_engine.pytorch.fp8 import FP8GlobalStateManager
from .multi_tensor_apply import multi_tensor_applier


def get_fp8_meta(fp8_tensor):
    """FP8 metadata getter."""
    if fp8_tensor._fp8_meta is None:
        raise RuntimeError("FP8 meta data is not initialized.")

    fp8_meta_key = FP8GlobalStateManager.get_meta_tensor_key(
        forward=fp8_tensor._fp8_meta_forward,
    )

    fp8_meta_index = fp8_tensor._fp8_meta_index
    scale = fp8_tensor._fp8_meta[fp8_meta_key].scale[fp8_meta_index]
    amax = fp8_tensor._fp8_meta[fp8_meta_key].amax_history[0][fp8_meta_index]
    scale_inv = fp8_tensor._scale_inv
    return scale, amax, scale_inv


class FusedAdam(torch.optim.Optimizer):
    """Implements Adam algorithm.

    Currently GPU-only.

    This version of fused Adam implements 2 fusions.

      * Fusion of the Adam update's elementwise operations
      * A multi-tensor apply launch that batches the elementwise updates applied to
        all the model's parameters into one or a few kernel launches.

    :class:`te.optimizers.FusedAdam` may be used as a drop-in replacement for ``torch.optim.AdamW``,
    or ``torch.optim.Adam`` with ``adam_w_mode=False``::

        opt = te.optimizers.FusedAdam(model.parameters(), lr = ....)
        ...
        opt.step()

    :class:`te.optimizers.FusedAdam` may be used with or without Amp.

    Adam was been proposed in `Adam: A Method for Stochastic Optimization`_.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups.
        lr (float, optional): learning rate. (default: 1e-3)
        bias_correction (bool, optional): apply correction factor to
            moment estimates. (default: True)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square. (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability. (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_
            (default: False) NOT SUPPORTED in FusedAdam!
        adam_w_mode (boolean, optional): Apply L2 regularization or weight decay
            True for decoupled weight decay(also known as AdamW) (default: True)
        set_grad_none (bool, optional): whether set grad to None when zero_grad()
            method is called. (default: True)
        capturable (bool, optional): whether to use the version of the optimizer
            that can be used with CUDA Graphs. (default: False)
        master_weights (list of torch.Tensor, optional): master weights to use
            for mixed precision training. If provided, the optimizer will update
            the master weights and then cast the master weights to the model weights.
            If not provided, the optimizer will update the model weights directly.
            (default: None)

    .. _Adam - A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(
        self,
        params,
        lr=1e-3,
        bias_correction=True,
        betas=(0.9, 0.999),
        eps=1e-8,
        adam_w_mode=True,
        weight_decay=0.0,
        amsgrad=False,
        set_grad_none=True,
        capturable=False,
        master_weights=None,
    ):

        if amsgrad:
            raise RuntimeError("FusedAdam does not support the AMSGrad variant.")

        # If the optimizer is capturable then LR should be a tensor (on GPU)
        lr = torch.tensor(lr, dtype=torch.float32) if capturable else lr
        defaults = {
            "lr": lr,
            "bias_correction": bias_correction,
            "betas": betas,
            "eps": eps,
            "weight_decay": weight_decay,
        }
        super().__init__(params, defaults)
        self.adam_w_mode = 1 if adam_w_mode else 0
        self.set_grad_none = set_grad_none

        self.capturable = capturable

        if master_weights is not None:
            assert isinstance(master_weights, list), "master_weights must be a list if provided"
        self.master_weights = master_weights

        if capturable:
            for idx, group in enumerate(self.param_groups):
                if len(group["params"]) == 0:
                    continue
                device = group["params"][0].device
                for item in ["lr"]:
                    self.param_groups[idx][item] = group[item].to(device=device)

            self._step_supports_amp_scaling = True

        # Skip buffer
        self._dummy_overflow_buf = torch.tensor([0], dtype=torch.int, device="cuda")
        self.multi_tensor_adam = tex.multi_tensor_adam
        self.multi_tensor_adam_fp8 = tex.multi_tensor_adam_fp8
        self.multi_tensor_adam_capturable = tex.multi_tensor_adam_capturable
        self.multi_tensor_adam_capturable_master = tex.multi_tensor_adam_capturable_master

    def zero_grad(self):
        # pylint: disable=missing-function-docstring
        if self.set_grad_none:
            for group in self.param_groups:
                for p in group["params"]:
                    p.grad = None
        else:
            super().zero_grad()

    def step(self, closure=None, grad_scaler=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
            grad_scaler (torch.cuda.amp.GradScaler, optional):
                gradient scaler (default: None)
        """
        loss = None
        if closure is not None:
            loss = closure()

        master_param_idx = 0

        for group in self.param_groups:
            if len(group["params"]) == 0:
                continue
            device = group["params"][0].device
            bias_correction = 1 if group["bias_correction"] else 0
            beta1, beta2 = group["betas"]

            # assume same step across group now to simplify things
            # per parameter step can be easily support by making it tensor, or pass list into kernel
            if "step" in group:
                group["step"] += (
                    1 if not self.capturable else (self._dummy_overflow_buf != 1).to(torch.int)
                )
            else:
                group["step"] = (
                    1 if not self.capturable else torch.tensor([1], dtype=torch.int, device=device)
                )

            # create lists for multi-tensor apply
            p_main_of_fp8_model = []
            p_main_of_f16_model = []
            g_of_fp8_model = []
            g_of_f16_model = []
            g_of_f32_model = []
            m_of_fp8_model = []
            m_of_f16_model = []
            m_of_f32_model = []
            v_of_fp8_model = []
            v_of_f16_model = []
            v_of_f32_model = []
            p_fp8_model = []
            p_f16_model = []
            p_f32_model = []
            # fp8 meta
            scales = []
            amaxes = []
            scale_invs = []

            # Only used when extra params include fp8 tensors. Otherwise, it doesn't matter what the out_dtype is.
            out_dtype = tex.DType.kFloat32

            has_fp16 = False
            has_bf16 = False

            for p in group["params"]:
                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    # Exponential moving average of gradient values
                    state["exp_avg"] = torch.zeros_like(p.data).float()
                    # Exponential moving average of squared gradient values
                    state["exp_avg_sq"] = torch.zeros_like(p.data).float()
                    # Master weights
                    if self.master_weights and p.dtype != torch.float32:
                        # model weights can be fp32/bf16/fp16/fp8
                        # If it's fp32, it has no corresponding master weights
                        state["master_param"] = self.master_weights[master_param_idx]
                        master_param_idx += 1
                        assert (
                            state["master_param"].shape == p.shape
                        ), "Master weights shape must match model weights shape"

                p_master = state.get("master_param", None)
                p_grad = p.grad

                if self.master_weights and p_master is not None and p_master.grad is not None:
                    p_grad = p_master.grad

                if p_grad is None:
                    continue
                if p_grad.data.is_sparse:
                    raise RuntimeError("FusedAdam does not support sparse gradients.")

                if isinstance(p, Float8Tensor):
                    out_dtype = p._fp8_dtype
                    p_fp8_model.append(p._data.data)
                    scale, amax, scale_inv = get_fp8_meta(p)
                    scales.append(scale)
                    amaxes.append(amax)
                    scale_invs.append(scale_inv)
                    if self.master_weights:
                        p_main_of_fp8_model.append(p_master.data)
                    g_of_fp8_model.append(p_grad.data)
                    m_of_fp8_model.append(state["exp_avg"])
                    v_of_fp8_model.append(state["exp_avg_sq"])
                elif p.dtype in [torch.float16, torch.bfloat16]:
                    has_fp16 = has_fp16 or p.dtype == torch.float16
                    has_bf16 = has_bf16 or p.dtype == torch.bfloat16
                    p_f16_model.append(p.data)
                    if self.master_weights:
                        p_main_of_f16_model.append(p_master.data)
                    g_of_f16_model.append(p_grad.data)
                    m_of_f16_model.append(state["exp_avg"])
                    v_of_f16_model.append(state["exp_avg_sq"])
                elif p.dtype == torch.float32:
                    p_f32_model.append(p.data)
                    g_of_f32_model.append(p_grad.data)
                    m_of_f32_model.append(state["exp_avg"])
                    v_of_f32_model.append(state["exp_avg_sq"])
                else:
                    raise RuntimeError("FusedAdam only support model weights in fp16/bf16 and fp8")

                if self.capturable and len(p_fp8_model) > 0:
                    raise RuntimeError(
                        "FusedAdam does not support FP8 model weights with capturable=True."
                    )

                if has_fp16 and has_bf16:
                    # simple to add support for this, but not needed for now
                    raise RuntimeError(
                        "FusedAdam does not support a mix of float16 and bfloat16 model weights."
                    )

            def apply_multi_tensor_adam(adam_func, tensor_lists, inv_scale=None, out_dtype=None):
                # Closures defined in a loop can have unexpected
                # behavior when called outside the loop. However, this
                # function is called in the same loop iteration as it
                # is defined.
                # pylint: disable=cell-var-from-loop
                inv_scale_arg = () if inv_scale is None else (inv_scale,)
                out_dtype_arg = () if out_dtype is None else (out_dtype,)
                multi_tensor_applier(
                    adam_func,
                    self._dummy_overflow_buf,
                    tensor_lists,
                    group["lr"],
                    beta1,
                    beta2,
                    group["eps"],
                    group["step"],
                    self.adam_w_mode,
                    bias_correction,
                    group["weight_decay"],
                    *inv_scale_arg,
                    *out_dtype_arg,
                )

            if self.capturable:
                # If the optimizer is capturable, then if there's a grad scaler it works
                # on the GPU + a different multi_tensor_applier should be called

                # overflow check of gradients
                found_inf = (
                    grad_scaler._check_inf_per_device(self)[device]
                    if grad_scaler is not None
                    else torch.zeros((1,), device=device)
                )
                self._dummy_overflow_buf.copy_(found_inf)

                # get unscale scale factor
                scale, inv_scale = None, None
                if grad_scaler:
                    scale = grad_scaler._get_scale_async()
                    inv_scale = scale.double().reciprocal().float()
                else:
                    scale = torch.ones((1,), device=device)
                    inv_scale = torch.ones((1,), device=device)

                if self.master_weights:
                    if len(p_f16_model) > 0:
                        tensor_lists = [
                            g_of_f16_model,
                            p_f16_model,
                            m_of_f16_model,
                            v_of_f16_model,
                            p_main_of_f16_model,
                        ]
                        apply_multi_tensor_adam(
                            self.multi_tensor_adam_capturable_master, tensor_lists, inv_scale
                        )
                    if len(p_f32_model) > 0:
                        tensor_lists = [
                            g_of_f32_model,
                            p_f32_model,
                            m_of_f32_model,
                            v_of_f32_model,
                        ]
                        apply_multi_tensor_adam(
                            self.multi_tensor_adam_capturable, tensor_lists, inv_scale
                        )
                else:
                    if len(p_f16_model) > 0:
                        tensor_lists = [g_of_f16_model, p_f16_model, m_of_f16_model, v_of_f16_model]
                        apply_multi_tensor_adam(
                            self.multi_tensor_adam_capturable, tensor_lists, inv_scale
                        )
                    if len(p_f32_model) > 0:
                        tensor_lists = [g_of_f32_model, p_f32_model, m_of_f32_model, v_of_f32_model]
                        apply_multi_tensor_adam(
                            self.multi_tensor_adam_capturable, tensor_lists, inv_scale
                        )

            elif self.master_weights:  # and self.capturable=False
                if len(p_f16_model) > 0:
                    tensor_lists = [
                        g_of_f16_model,
                        p_f16_model,
                        m_of_f16_model,
                        v_of_f16_model,
                        p_main_of_f16_model,
                    ]
                    apply_multi_tensor_adam(self.multi_tensor_adam, tensor_lists)
                if len(p_fp8_model) > 0:
                    tensor_lists = [
                        g_of_fp8_model,
                        p_fp8_model,
                        m_of_fp8_model,
                        v_of_fp8_model,
                        p_main_of_fp8_model,
                        scales,
                        amaxes,
                        scale_invs,
                    ]
                    apply_multi_tensor_adam(self.multi_tensor_adam_fp8, tensor_lists, out_dtype)
                if len(p_f32_model) > 0:
                    tensor_lists = [
                        g_of_f32_model,
                        p_f32_model,
                        m_of_f32_model,
                        v_of_f32_model,
                    ]
                    apply_multi_tensor_adam(self.multi_tensor_adam, tensor_lists)
            else:  # self.master_weights=False and self.capturable=False
                if len(p_f16_model) > 0:
                    tensor_lists = [g_of_f16_model, p_f16_model, m_of_f16_model, v_of_f16_model]
                    apply_multi_tensor_adam(self.multi_tensor_adam, tensor_lists)
                if len(p_f32_model) > 0:
                    tensor_lists = [g_of_f32_model, p_f32_model, m_of_f32_model, v_of_f32_model]
                    apply_multi_tensor_adam(self.multi_tensor_adam, tensor_lists)

        return loss
