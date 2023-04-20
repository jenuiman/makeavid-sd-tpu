import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange

class Pseudo3DConv(nn.Module):
    def __init__(
        self,
        dim,
        dim_out,
        kernel_size,
        **kwargs
    ):
        super().__init__()

        self.spatial_conv = nn.Conv2d(dim, dim_out, kernel_size, **kwargs)
        self.temporal_conv = nn.Conv1d(dim_out, dim_out, kernel_size, padding=kernel_size // 2)
        self.temporal_conv = nn.Conv1d(dim_out, dim_out, 3, padding=1)

        nn.init.dirac_(self.temporal_conv.weight.data) # initialized to be identity
        nn.init.zeros_(self.temporal_conv.bias.data)

    def forward(
        self,
        x,
        convolve_across_time = True
    ):
        b, c, *_, h, w = x.shape

        is_video = x.ndim == 5
        convolve_across_time &= is_video

        if is_video:
            x = rearrange(x, 'b c f h w -> (b f) c h w')

        #with torch.no_grad():
        #    x = self.spatial_conv(x)
        x = self.spatial_conv(x)

        if is_video:
            x = rearrange(x, '(b f) c h w -> b c f h w', b = b)
            b, c, *_, h, w = x.shape
        
        if not convolve_across_time:
            return x

        if is_video:
            x = rearrange(x, 'b c f h w -> (b h w) c f')
            x = self.temporal_conv(x)
            x = rearrange(x, '(b h w) c f -> b c f h w', h = h, w = w)
        return x

class Upsample2D(nn.Module):
    """
    An upsampling layer with an optional convolution.

    Parameters:
        channels: channels in the inputs and outputs.
        use_conv: a bool determining if a convolution is applied.
        use_conv_transpose:
        out_channels:
    """

    def __init__(self, channels, use_conv=False, use_conv_transpose=False, out_channels=None, name="conv"):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.use_conv_transpose = use_conv_transpose
        self.name = name

        conv = None
        if use_conv_transpose:
            conv = nn.ConvTranspose2d(channels, self.out_channels, 4, 2, 1)
        elif use_conv:
            conv = Pseudo3DConv(self.channels, self.out_channels, 3, padding=1)

        # TODO(Suraj, Patrick) - clean up after weight dicts are correctly renamed
        if name == "conv":
            self.conv = conv
        else:
            self.Conv2d_0 = conv

    def forward(self, hidden_states, output_size=None):
        assert hidden_states.shape[1] == self.channels

        if self.use_conv_transpose:
            return self.conv(hidden_states)

        # Cast to float32 to as 'upsample_nearest2d_out_frame' op does not support bfloat16
        # TODO(Suraj): Remove this cast once the issue is fixed in PyTorch
        # https://github.com/pytorch/pytorch/issues/86679
        dtype = hidden_states.dtype
        if dtype == torch.bfloat16:
            hidden_states = hidden_states.to(torch.float32)

        # upsample_nearest_nhwc fails with large batch sizes. see https://github.com/huggingface/diffusers/issues/984
        if hidden_states.shape[0] >= 64:
            hidden_states = hidden_states.contiguous()

        b, c, *_, h, w = hidden_states.shape

        is_video = hidden_states.ndim == 5

        if is_video:
            hidden_states = rearrange(hidden_states, 'b c f h w -> (b f) c h w')

        # if `output_size` is passed we force the interpolation output
        # size and do not make use of `scale_factor=2`
        if output_size is None:
            hidden_states = F.interpolate(hidden_states, scale_factor=2.0, mode="nearest")
        else:
            hidden_states = F.interpolate(hidden_states, size=output_size, mode="nearest")

        if is_video:
            hidden_states = rearrange(hidden_states, '(b f) c h w -> b c f h w', b = b)

        # If the input is bfloat16, we cast back to bfloat16
        if dtype == torch.bfloat16:
            hidden_states = hidden_states.to(dtype)

        # TODO(Suraj, Patrick) - clean up after weight dicts are correctly renamed
        if self.use_conv:
            if self.name == "conv":
                hidden_states = self.conv(hidden_states)
            else:
                hidden_states = self.Conv2d_0(hidden_states)

        return hidden_states


class Downsample2D(nn.Module):
    """
    A downsampling layer with an optional convolution.

    Parameters:
        channels: channels in the inputs and outputs.
        use_conv: a bool determining if a convolution is applied.
        out_channels:
        padding:
    """

    def __init__(self, channels, use_conv=False, out_channels=None, padding=1, name="conv"):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.padding = padding
        stride = 2
        self.name = name

        if use_conv:
            conv = Pseudo3DConv(self.channels, self.out_channels, 3, stride=stride, padding=padding)
        else:
            assert self.channels == self.out_channels
            conv = nn.AvgPool2d(kernel_size=stride, stride=stride)

        # TODO(Suraj, Patrick) - clean up after weight dicts are correctly renamed
        if name == "conv":
            self.Conv2d_0 = conv
            self.conv = conv
        elif name == "Conv2d_0":
            self.conv = conv
        else:
            self.conv = conv

    def forward(self, hidden_states):
        assert hidden_states.shape[1] == self.channels
        if self.use_conv and self.padding == 0:
            pad = (0, 1, 0, 1)
            hidden_states = F.pad(hidden_states, pad, mode="constant", value=0)

        assert hidden_states.shape[1] == self.channels
        if self.use_conv:
            hidden_states = self.conv(hidden_states)
        else:
            b, c, *_, h, w = hidden_states.shape
            is_video = hidden_states.ndim == 5
            if is_video:
                hidden_states = rearrange(hidden_states, 'b c f h w -> (b f) c h w')
            hidden_states = self.conv(hidden_states)
            if is_video:
                hidden_states = rearrange(hidden_states, '(b f) c h w -> b c f h w', b = b)

        return hidden_states


class ResnetBlockPseudo3D(nn.Module):
    def __init__(
        self,
        *,
        in_channels,
        out_channels=None,
        conv_shortcut=False,
        dropout=0.0,
        temb_channels=512,
        groups=32,
        groups_out=None,
        pre_norm=True,
        eps=1e-6,
        time_embedding_norm="default",
        kernel=None,
        output_scale_factor=1.0,
        use_in_shortcut=None,
        up=False,
        down=False,
    ):
        super().__init__()
        self.pre_norm = pre_norm
        self.pre_norm = True
        self.in_channels = in_channels
        out_channels = in_channels if out_channels is None else out_channels
        self.out_channels = out_channels
        self.use_conv_shortcut = conv_shortcut
        self.time_embedding_norm = time_embedding_norm
        self.up = up
        self.down = down
        self.output_scale_factor = output_scale_factor
        print('OUTPUT_SCALE_FACTOR:', output_scale_factor)

        if groups_out is None:
            groups_out = groups

        self.norm1 = torch.nn.GroupNorm(num_groups=groups, num_channels=in_channels, eps=eps, affine=True)

        self.conv1 = Pseudo3DConv(in_channels, out_channels, kernel_size=3, stride=1, padding=1)

        if temb_channels is not None:
            self.time_emb_proj = torch.nn.Linear(temb_channels, out_channels)
        else:
            self.time_emb_proj = None

        self.norm2 = torch.nn.GroupNorm(num_groups=groups_out, num_channels=out_channels, eps=eps, affine=True)
        self.dropout = torch.nn.Dropout(dropout)
        self.conv2 = Pseudo3DConv(out_channels, out_channels, kernel_size=3, stride=1, padding=1)

        self.nonlinearity = nn.SiLU()

        self.upsample = self.downsample = None
        if self.up:
            self.upsample = Upsample2D(in_channels, use_conv=False)
        elif self.down:
            self.downsample = Downsample2D(in_channels, use_conv=False, padding=1, name="op")

        self.use_in_shortcut = self.in_channels != self.out_channels if use_in_shortcut is None else use_in_shortcut

        self.conv_shortcut = None
        if self.use_in_shortcut:
            self.conv_shortcut = Pseudo3DConv(in_channels, out_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, input_tensor, temb):
        hidden_states = input_tensor

        hidden_states = self.norm1(hidden_states)
        hidden_states = self.nonlinearity(hidden_states)

        if self.upsample is not None:
            # upsample_nearest_nhwc fails with large batch sizes. see https://github.com/huggingface/diffusers/issues/984
            if hidden_states.shape[0] >= 64:
                input_tensor = input_tensor.contiguous()
                hidden_states = hidden_states.contiguous()
            input_tensor = self.upsample(input_tensor)
            hidden_states = self.upsample(hidden_states)
        elif self.downsample is not None:
            input_tensor = self.downsample(input_tensor)
            hidden_states = self.downsample(hidden_states)

        hidden_states = self.conv1(hidden_states)

        if temb is not None:
            b, c, *_, h, w = hidden_states.shape
            is_video = hidden_states.ndim == 5
            if is_video:
                b, c, f, h, w = hidden_states.shape
                hidden_states = rearrange(hidden_states, 'b c f h w -> (b f) c h w')
                temb = self.time_emb_proj(self.nonlinearity(temb))[:, :, None, None]
                hidden_states = hidden_states + temb.repeat_interleave(f, 0)
                hidden_states = rearrange(hidden_states, '(b f) c h w -> b c f h w', b=b)
            else:
                temb = self.time_emb_proj(self.nonlinearity(temb))[:, :, None, None]
                hidden_states = hidden_states + temb

        hidden_states = self.norm2(hidden_states)
        hidden_states = self.nonlinearity(hidden_states)

        hidden_states = self.dropout(hidden_states)
        hidden_states = self.conv2(hidden_states)

        if self.conv_shortcut is not None:
            input_tensor = self.conv_shortcut(input_tensor)

        output_tensor = (input_tensor + hidden_states) / self.output_scale_factor

        return output_tensor

