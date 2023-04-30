
from typing import Any, Union, Tuple, List, Dict
import os
import gc
from functools import partial

import jax
import jax.numpy as jnp
import numpy as np

from flax.core.frozen_dict import FrozenDict
from flax import jax_utils
from flax.training.common_utils import shard
from PIL import Image
import einops

from diffusers import FlaxAutoencoderKL
from diffusers import (
        FlaxDDIMScheduler,
        FlaxDDPMScheduler,
        FlaxPNDMScheduler,
        FlaxLMSDiscreteScheduler,
        FlaxDPMSolverMultistepScheduler,
        FlaxKarrasVeScheduler,
        FlaxScoreSdeVeScheduler
)

from transformers import FlaxCLIPTextModel, CLIPTokenizer

from flax_impl import UNetPseudo3DConditionModel

SchedulerType = Union[
        FlaxDDIMScheduler,
        FlaxDDPMScheduler,
        FlaxPNDMScheduler,
        FlaxLMSDiscreteScheduler,
        FlaxDPMSolverMultistepScheduler,
        FlaxKarrasVeScheduler,
        FlaxScoreSdeVeScheduler
]

class InferenceUNetPseudo3D:
    def __init__(self,
            model_path: str,
            scheduler_cls: SchedulerType = FlaxDDIMScheduler,
            dtype: jnp.dtype = jnp.float16,
            low_vram: bool = True
    ) -> None:
        self.low_vram = low_vram
        self.dtype = dtype
        self.model_path = model_path
        def dtypestr(x: jnp.dtype):
            if x == jnp.float32: return 'float32'
            elif x == jnp.float16: return 'float16'
            elif x == jnp.bfloat16: return 'bfloat16'
            else: raise
        def castto(dtype, m, x):
            if dtype == jnp.float32: return m.to_fp32(x)
            elif dtype == jnp.float16: return m.to_fp16(x)
            elif dtype == jnp.bfloat16: return m.to_bf16(x)
            else: raise
        self.params: Dict[str, FrozenDict[str, Any]] = {}
        unet, unet_params = UNetPseudo3DConditionModel.from_pretrained(
                self.model_path,
                subfolder = 'unet',
                from_pt = False,
                sample_size = (64, 64),
                dtype = dtypestr(self.dtype),
                param_dtype = dtypestr(self.dtype),
                use_memory_efficient_attention = True
        )
        self.unet: UNetPseudo3DConditionModel = unet
        unet_params = castto(self.dtype, self.unet, unet_params)
        self.params['unet'] = FrozenDict(unet_params)
        del unet_params
        vae, vae_params = FlaxAutoencoderKL.from_pretrained(
                self.model_path,
                subfolder = 'vae',
                from_pt = True,
                dtype = self.dtype
        )
        self.vae: FlaxAutoencoderKL = vae
        vae_params = castto(self.dtype, self.vae, vae_params)
        self.params['vae'] = FrozenDict(vae_params)
        del vae_params
        text_encoder = FlaxCLIPTextModel.from_pretrained(
                self.model_path,
                subfolder = 'text_encoder',
                from_pt = True,
                dtype = self.dtype
        )
        text_encoder_params = text_encoder.params
        del text_encoder._params
        text_encoder_params = castto(self.dtype, text_encoder, text_encoder_params)
        self.text_encoder: FlaxCLIPTextModel = text_encoder
        self.params['text_encoder'] = FrozenDict(text_encoder_params)
        del text_encoder_params
        self.tokenizer: CLIPTokenizer = CLIPTokenizer.from_pretrained(
                self.model_path,
                subfolder = 'tokenizer'
        )
        scheduler, scheduler_state = scheduler_cls.from_pretrained(
                self.model_path,
                subfolder = 'scheduler',
                #dtype = self.dtype
                dtype = jnp.float32
        )
        self.scheduler: scheduler_cls = scheduler
        self.params['scheduler'] = scheduler_state
        self.vae_scale_factor: int = int(2 ** (len(self.vae.config.block_out_channels) - 1))
        self.device_count = jax.device_count()
        gc.collect()

    def set_scheduler(self, scheduler_cls: SchedulerType) -> None:
        scheduler, scheduler_state = scheduler_cls.from_pretrained(
                self.model_path,
                subfolder = 'scheduler',
                dtype = jnp.float32
        )
        self.scheduler: scheduler_cls = scheduler
        self.params['scheduler'] = scheduler_state

    def prepare_inputs(self,
            prompt: List[str],
            neg_prompt: List[str],
            hint_image: List[Image.Image],
            mask_image: List[Image.Image],
            width: int,
            height: int
    ) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]: # prompt, neg_prompt, hint_image, mask_image
        tokens = self.tokenizer(
            prompt,
            truncation = True,
            return_overflowing_tokens = False,
            max_length = 77, #self.text_encoder.config.max_length defaults to 20 if its not in the config smh
            padding = 'max_length',
            return_tensors = 'np'
        ).input_ids
        tokens = jnp.array(tokens, dtype = jnp.int32)
        neg_tokens = self.tokenizer(
            neg_prompt,
            truncation = True,
            return_overflowing_tokens = False,
            max_length = 77,
            padding = 'max_length',
            return_tensors = 'np'
        ).input_ids
        neg_tokens = jnp.array(neg_tokens, dtype = jnp.int32)
        for i,im in enumerate(hint_image):
            if im.size != (width, height):
                hint_image[i] = hint_image[i].resize((width, height), resample = Image.Resampling.LANCZOS)
        for i,im in enumerate(mask_image):
            if im.size != (width, height):
                mask_image[i] = mask_image[i].resize((width, height), resample = Image.Resampling.LANCZOS)
        # b,h,w,c | c == 3
        hint = jnp.concatenate(
                [ jnp.expand_dims(np.asarray(x.convert('RGB')), axis = 0) for x in hint_image ],
                axis = 0
        ).astype(jnp.float32)
        # scale -1,1
        hint = (hint / 255) * 2 - 1
        # b,h,w,c | c == 1
        mask = jnp.concatenate(
                [ jnp.expand_dims(np.asarray(x.convert('L')), axis = (0, -1)) for x in mask_image ],
                axis = 0
        ).astype(jnp.float32)
        # scale -1,1
        mask = (mask / 255) * 2 - 1
        # binarize mask
        mask = mask.at[mask < 0.5].set(0)
        mask = mask.at[mask >= 0.5].set(1)
        # mask
        hint = hint * (mask < 0.5)
        # b,h,w,c -> b,c,h,w
        hint = hint.transpose((0,3,1,2))
        mask = mask.transpose((0,3,1,2))
        return tokens, neg_tokens, hint, mask

    def generate(self,
            prompt: Union[str, List[str]],
            hint_image: Union[Image.Image, List[Image.Image]],
            inference_steps: int,
            mask_image: Union[Image.Image, List[Image.Image], None] = None,
            neg_prompt: Union[str, List[str]] = '',
            cfg: float = 10.0,
            num_frames: int = 24,
            width: int = 512,
            height: int = 512,
            seed: int = 0
    ) -> List[List[Image.Image]]:
        assert inference_steps > 0, f'number of inference steps must be > 0 but is {inference_steps}'
        assert num_frames > 0, f'number of frames must be > 0 but is {num_frames}'
        assert width % 32 == 0, f'width must be divisible by 32 but is {width}'
        assert height % 32 == 0, f'height must be divisible by 32 but is {height}'
        if isinstance(prompt, str):
            prompt = [ prompt ]
        batch_size = len(prompt)
        assert batch_size % self.device_count == 0, f'batch size must be multiple of {self.device_count}'
        if isinstance(hint_image, Image.Image):
            hint_image = [ hint_image ] * batch_size
        assert len(hint_image) == batch_size, f'number of hint images must be equal to batch size {batch_size} but is {len(hint_image)}'
        if mask_image is None:
            mask_image = Image.new('L', hint_image[0].size, color = 0)
        if isinstance(mask_image, Image.Image):
            mask_image = [ mask_image ] * batch_size
        assert len(mask_image) == batch_size, f'number of mask images must be equal to batch size {batch_size} but is {len(mask_image)}'
        if isinstance(neg_prompt, str):
            neg_prompt = [ neg_prompt ] * batch_size
        assert len(neg_prompt) == batch_size, f'number of negative prompts must be equal to batch size {batch_size} but is {len(neg_prompt)}'
        tokens, neg_tokens, hint, mask = self.prepare_inputs(
                prompt = prompt,
                neg_prompt = neg_prompt,
                hint_image = hint_image,
                mask_image = mask_image,
                width = width,
                height = height
        )
        # splitting rngs kills the crab
        #rng = jax.random.PRNGKey(seed)
        #rngs = jax.random.split(rng, self.device_count)
        rngs = jnp.array([ jax.random.PRNGKey(seed + i) for i in range(self.device_count) ])
        params = jax_utils.replicate(self.params)
        tokens = shard(tokens)
        neg_tokens = shard(neg_tokens)
        hint = shard(hint)
        mask = shard(mask)
        images = _p_generate(self,
            tokens,
            neg_tokens,
            hint,
            mask,
            inference_steps,
            num_frames,
            height,
            width,
            cfg,
            rngs,
            params
        )
        if images.ndim == 5:
            images = einops.rearrange(images, 'd f c h w -> (d f) h w c')
        else:
            images = einops.rearrange(images, 'f c h w -> f h w c')
        # to cpu
        images = np.array(images)
        images = [ Image.fromarray(x) for x in images ]
        return images

    def _generate(self,
            tokens: jnp.ndarray,
            neg_tokens: jnp.ndarray,
            hint: jnp.ndarray,
            mask: jnp.ndarray,
            inference_steps: int,
            num_frames,
            height,
            width,
            cfg: float,
            rng: jax.random.KeyArray,
            params: Union[Dict[str, Any], FrozenDict[str, Any]]
    ) -> List[Image.Image]:
        batch_size = tokens.shape[0]
        latent_h = height // self.vae_scale_factor
        latent_w = width // self.vae_scale_factor
        latent_shape = (
                batch_size,
                self.vae.config.latent_channels,
                num_frames,
                latent_h,
                latent_w
        )
        encoded_prompt = self.text_encoder(tokens, params = params['text_encoder'])[0]
        encoded_neg_prompt = self.text_encoder(neg_tokens, params = params['text_encoder'])[0]
        hint = self.vae.apply(
                {'params': params['vae']},
                hint,
                method = self.vae.encode
        ).latent_dist.mean * self.vae.config.scaling_factor
        # NOTE vae keeps channels last for encode, but rearranges to channels first for decode
        # b0 h1 w2 c3 -> b0 c3 h1 w2
        hint = hint.transpose((0, 3, 1, 2))
        hint = jnp.expand_dims(hint, axis = 2).repeat(num_frames, axis = 2)
        mask = jax.image.resize(mask, (*mask.shape[:-2], *hint.shape[-2:]), method = 'nearest')
        mask = jnp.expand_dims(mask, axis = 2).repeat(num_frames, axis = 2)
        # NOTE jax normal distribution is shit with float16 + bfloat16
        # SEE https://github.com/google/jax/discussions/13798
        # generate random at float32 then cast to dtype
        latents = jax.random.normal(rng, shape = latent_shape, dtype = jnp.float32) * params['scheduler'].init_noise_sigma
        scheduler_state = self.scheduler.set_timesteps(params['scheduler'], num_inference_steps = inference_steps, shape = latents.shape)

        def sample_loop(step, args):
            latents, scheduler_state = args
            t = scheduler_state.timesteps[step]#jnp.array(scheduler_state.timesteps, dtype = jnp.int32)[step]
            t = jnp.broadcast_to(t, latents.shape[0])
            latents_input = self.scheduler.scale_model_input(scheduler_state, latents, t)
            latents_input = jnp.concatenate([latents_input, mask, hint], axis = 1)
            noise_pred = self.unet.apply(
                    { 'params': params['unet'] },
                    latents_input,
                    t,
                    encoded_prompt
            ).sample
            noise_pred_uncond = self.unet.apply(
                    { 'params': params['unet'] },
                    latents_input,
                    t,
                    encoded_neg_prompt
            ).sample
            noise_pred = noise_pred_uncond + cfg * (noise_pred - noise_pred_uncond)
            latents, scheduler_state = self.scheduler.step(
                    scheduler_state,
                    noise_pred.astype(jnp.float32),
                    t,
                    latents.astype(jnp.float32)
            ).to_tuple()
            return latents, scheduler_state

        latents, _ = jax.lax.fori_loop(0, inference_steps, sample_loop, (latents, scheduler_state))
        latents = 1 / self.vae.config.scaling_factor * latents
        latents = einops.rearrange(latents, 'b c f h w -> (b f) c h w')
        num_images = len(latents)
        if self.low_vram:
            images_out = jnp.zeros(
                    (
                            num_images,
                            self.vae.config.out_channels,
                            height,
                            width
                    ),
                    dtype = self.dtype
            )
            def decode_loop(step, images_out):
                # NOTE vae keeps channels last for encode, but rearranges to channels first for decode
                im = self.vae.apply(
                        { 'params': params['vae'] },
                        jnp.expand_dims(latents[step], axis=0),
                        method = self.vae.decode
                ).sample
                images_out = images_out.at[step].set(im[0])
                return images_out
            images_out = jax.lax.fori_loop(0, num_images, decode_loop, images_out)
        else:
            images_out = self.vae.apply(
                    { 'params': params['vae'] },
                    latents,
                    method = self.vae.decode
            ).sample
        images_out = ((images_out / 2 + 0.5).clip(0, 1) * 255).round().clip(0,255).astype(jnp.uint8)
        return images_out

# Static argnums are inference_class, num_inference_steps, height, width. A change would trigger recompilation.
# Non-static args are (sharded) input tensors mapped over their first dimension (hence, `0`).
@partial(
        jax.pmap,
        in_axes = (
                None,   #  0 inference_class
                0,      #  1 tokens
                0,      #  2 neg_tokens
                0,      #  3 hint
                0,      #  4 mask
                None,   #  5 inference_steps
                None,   #  6 num_frames
                None,   #  7 height
                None,   #  8 width
                None,   #  9 cfg
                0,      # 10 rng
                0       # 11 params
        ),
        static_broadcasted_argnums = (
                0,      # inference_class
                5,      # inference_steps
                6,      # num_frames
                7,      # height
                8       # width
        )
)
def _p_generate(
        inference_class: InferenceUNetPseudo3D,
        tokens,
        neg_tokens,
        hint,
        mask,
        inference_steps,
        num_frames,
        height,
        width,
        cfg,
        rng,
        params
):
    return inference_class._generate(
            tokens,
            neg_tokens,
            hint,
            mask,
            inference_steps,
            num_frames,
            height,
            width,
            cfg,
            rng,
            params
    )


# example
# hint_image = Image.open('testimage6.png')
# mask_image = Image.new('L', hint_image.size, color = 0)
# prompt = "Dancing man funny break dance, happy rhythmically waving his arms against grey textured street wall. Playful crazy man dance emotions. Breakdancing, street dancing, hiphop, freestyle dance. Lifestyle"
# model = InferenceUNetPseudo3D(
#         model_path = './model/ep38',
#         scheduler_cls = FlaxDDIMScheduler,#FlaxPNDMScheduler,#FlaxDDIMScheduler,
#         dtype = jnp.float16,
#         low_vram = True
# )
# images = model.generate(
#         prompt = [prompt]*model.device_count,
#         hint_image = hint_image,
#         inference_steps = 100,
#         mask_image = mask_image,
#         cfg = 12,
#         seed = 1
# )
