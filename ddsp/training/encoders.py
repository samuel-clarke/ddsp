# Copyright 2020 The DDSP Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Library of encoder objects."""

import ddsp
from ddsp import spectral_ops
from ddsp.training import nn
import gin
import numpy as np
import tensorflow.compat.v2 as tf

tfkl = tf.keras.layers


# ------------------ Encoders --------------------------------------------------
class Encoder(tfkl.Layer):
  """Base class to implement any encoder.

  Users should override compute_z() to define the actual encoder structure.
  Optionally, set infer_f0 to True and override compute_f0.
  Hyper-parameters will be passed through the constructor.
  """

  def __init__(self, f0_encoder=None, other_encoders=None, name='encoder'):
    super().__init__(name=name)
    self.f0_encoder = f0_encoder
    self.other_encoders = other_encoders

  def call(self, conditioning):
    """Updates conditioning with z and (optionally) f0."""
    if self.f0_encoder:
      # Use frequency conditioning created by the f0_encoder, not the dataset.
      # Overwrite `f0_scaled` and `f0_hz`. 'f0_scaled' is a value in [0, 1]
      # corresponding to midi values [0..127]
      conditioning['f0_scaled'] = self.f0_encoder(conditioning)
      conditioning['f0_hz'] = ddsp.core.midi_to_hz(
          conditioning['f0_scaled'] * 127.0)

    z = self.compute_z(conditioning)
    if 'f0_scaled' in conditioning:
      time_steps = int(conditioning['f0_scaled'].shape[1])
      z = self.expand_z(z, time_steps)
    elif hasattr(self, 'z_time_steps'):
      z = self.expand_z(z, self.z_time_steps)
    if self.other_encoders:
      for enc in self.other_encoders:
        z = self.concat_encoding(enc(conditioning)[enc.output_key], z)
    conditioning['z'] = z
    return conditioning

  def concat_encoding (self, enc, z):
    enc = self.expand_z(enc, z.shape[1])
    return tf.concat([z, enc], axis=2)

  def expand_z(self, z, time_steps):
    """Make sure z has same temporal resolution as other conditioning."""
    # Add time dim of z if necessary.
    if len(z.shape) == 2:
      z = z[:, tf.newaxis, :]
    # Expand time dim of z if necessary.
    z_time_steps = int(z.shape[1])
    if z_time_steps != time_steps:
      z = ddsp.core.resample(z, time_steps)
    return z

  def compute_z(self, conditioning):
    """Takes in conditioning dictionary, returns a latent tensor z."""
    raise NotImplementedError


@gin.register
class MfccTimeDistributedRnnEncoder(Encoder):
  """Use MFCCs as latent variables, distribute across timesteps."""

  def __init__(self,
               rnn_channels=512,
               rnn_type='gru',
               z_dims=32,
               mfcc_time_steps=250,
               z_time_steps=250,
               sample_rate=16000,
               f0_encoder=None,
               other_encoders=None,
               name='mfcc_time_distrbuted_rnn_encoder'):
    super().__init__(f0_encoder=f0_encoder, other_encoders=other_encoders, name=name)
    if mfcc_time_steps not in [63, 125, 250, 500, 1000]:
      raise ValueError(
          '`mfcc_time_steps` currently limited to 63,125,250,500 and 1000')
    self.z_audio_spec = {
        '63': {
            'fft_size': 2048,
            'overlap': 0.5
        },
        '125': {
            'fft_size': 1024,
            'overlap': 0.5
        },
        '250': {
            'fft_size': 1024,
            'overlap': 0.75
        },
        '500': {
            'fft_size': 512,
            'overlap': 0.75
        },
        '1000': {
            'fft_size': 256,
            'overlap': 0.75
        }
    }
    self.fft_size = self.z_audio_spec[str(mfcc_time_steps)]['fft_size']
    self.overlap = self.z_audio_spec[str(mfcc_time_steps)]['overlap']
    self.sample_rate = sample_rate
    if z_time_steps:
      print('Z time steps: %i'%z_time_steps)
      self.z_time_steps = z_time_steps

    # Layers.
    self.z_norm = nn.Normalize('instance')
    self.rnn = nn.Rnn(rnn_channels, rnn_type)
    self.tcnn = nn.temporal_cnn(rnn_channels, 7, causal=False)
    self.dense_out = tfkl.Dense(z_dims)

  def compute_z(self, conditioning):
    mfccs = spectral_ops.compute_mfcc(
        conditioning['audio'],
        sample_rate=self.sample_rate,
        lo_hz=4.0,
        hi_hz=16000.0,
        fft_size=self.fft_size,
        mel_bins=128,
        mfcc_bins=40,
        overlap=self.overlap,
        pad_end=True)

    # Normalize.
    z = self.z_norm(mfccs[:, :, tf.newaxis, :])[:, :, 0, :]
    # Run an RNN over the latents.
    z = self.rnn(z)
    # Run a tcnn over latents.
    z = self.tcnn(z)
    # Bounce down to compressed z dimensions.
    z = self.dense_out(z)
    return z


class F0Encoder(tfkl.Layer):
  """Mixin for F0 encoders."""

  def call(self, conditioning):
    return self.compute_f0(conditioning)

  def compute_f0(self, conditioning):
    """Takes in conditioning dictionary, returns fundamental frequency."""
    raise NotImplementedError

  def _compute_unit_midi(self, probs):
    """Computes the midi from a distribution over the unit interval."""
    # probs: [B, T, D]
    depth = int(probs.shape[-1])

    unit_midi_bins = tf.constant(
        1.0 * np.arange(depth).reshape((1, 1, -1)) / depth,
        dtype=tf.float32)  # [1, 1, D]

    f0_unit_midi = tf.reduce_sum(
        unit_midi_bins * probs, axis=-1, keepdims=True)  # [B, T, 1]
    return f0_unit_midi


@gin.register
class ResnetF0Encoder(F0Encoder):
  """Embeddings from resnet on spectrograms."""

  def __init__(self,
               size='large',
               f0_bins=128,
               spectral_fn=lambda x: spectral_ops.compute_mag(x, size=1024),
               name='resnet_f0_encoder'):
    super().__init__(name=name)
    self.f0_bins = f0_bins
    self.spectral_fn = spectral_fn

    # Layers.
    self.resnet = nn.ResNet(size=size)
    self.dense_out = tfkl.Dense(f0_bins)

  def compute_f0(self, conditioning):
    """Compute fundamental frequency."""
    mag = self.spectral_fn(conditioning['audio'])
    mag = mag[:, :, :, tf.newaxis]
    x = self.resnet(mag)

    # Collapse the frequency dimension
    x_shape = x.shape.as_list()
    y = tf.reshape(x, [x_shape[0], x_shape[1], -1])
    # Project to f0_bins
    y = self.dense_out(y)

    # treat the NN output as probability over midi values.
    # probs = tf.nn.softmax(y)  # softmax leads to NaNs
    probs = tf.nn.softplus(y) + 1e-3
    probs = probs / tf.reduce_sum(probs, axis=-1, keepdims=True)
    f0 = self._compute_unit_midi(probs)

    # Make same time resolution as original CREPE f0.
    n_timesteps = int(conditioning['f0_scaled'].shape[1])
    f0 = ddsp.core.resample(f0, n_timesteps)
    return f0

class ContextEncoder(tfkl.Layer):
  """Mixin for context encoders."""

  def call(self, conditioning):
    return self.compute_context(conditioning)

  def compute_context(self, conditioning):
    """Takes in conditioning dictionary, returns context."""
    raise NotImplementedError

@gin.register
class EmbeddingContextEncoder(ContextEncoder):
  """Embeddings from a dictionary of embeddings."""

  def __init__(self,
               vocab_size,
               vector_length,
               conditioning_key,
               output_key,
               name='embedding_context_encoder'):
    super().__init__(name=name)
    self.vocab_size = vocab_size
    self.vector_length = vector_length
    self.conditioning_key = conditioning_key
    self.output_key = output_key

    # Layers.
    self.embedding = nn.embedding(self.vocab_size, self.vector_length)

  def compute_context(self, conditioning):
    """Compute context from embedding."""
    return self.embedding(tf.cast(conditioning[self.conditioning_key], dtype=tf.int32))

  def call(self, conditioning):
    """Updates conditioning with embedding."""
    conditioning[self.output_key] = self.compute_context(conditioning)
    return conditioning

class MultiEncoder(Encoder):
  """Runs a list of encoders in order."""

  def __init__(self, encoder_list, name='multi_encoder'):
    super().__init__(name=name)
    self.encoder_list = encoder_list

  def call(self, conditioning):
    for enc in self.encoder_list:
      conditioning = enc(conditioning)
    return conditioning

# Transcribing Autoencoder Encoders --------------------------------------------
@gin.register
class ResnetSinusoidalEncoder(tfkl.Layer):
  """This encoder maps directly from audio to synthesizer parameters.

  EXPERIMENTAL

  It is equivalent of a base Encoder and Decoder together.
  """

  def __init__(self,
               output_splits=(('frequencies', 100 * 64),
                              ('amplitudes', 100),
                              ('noise_magnitudes', 60)),
               spectral_fn=spectral_ops.compute_logmel,
               size='tiny',
               name='resnet_sinusoidal_encoder'):
    super().__init__(name=name)
    self.output_splits = output_splits
    self.spectral_fn = spectral_fn

    # Layers.
    self.resnet = nn.ResNet(size=size)
    self.dense_outs = [tfkl.Dense(v[1]) for v in output_splits]

  def call(self, features):
    """Updates conditioning with z and (optionally) f0."""
    outputs = {}

    # [batch, 64000, 1]
    mag = self.spectral_fn(features['audio'])

    # [batch, 125, 229]
    mag = mag[:, :, :, tf.newaxis]
    x = self.resnet(mag)

    # [batch, 125, 8, 1024]
    # # Collapse the frequency dimension.
    x = tf.reshape(x, [int(x.shape[0]), int(x.shape[1]), -1])

    # [batch, 125, 8192]
    for layer, (key, _) in zip(self.dense_outs, self.output_splits):
      outputs[key] = layer(x)

    return outputs


@gin.register
class SinusoidalToHarmonicEncoder(tfkl.Layer):
  """Predicts harmonic controls from sinusoidal controls.

  EXPERIMENTAL
  """

  def __init__(self,
               fc_stack_layers=2,
               fc_stack_ch=256,
               rnn_ch=512,
               rnn_type='gru',
               n_harmonics=100,
               amp_scale_fn=ddsp.core.exp_sigmoid,
               f0_depth=64,
               hz_min=20.0,
               hz_max=1200.0,
               sample_rate=16000,
               name='sinusoidal_to_harmonic_encoder'):
    """Constructor."""
    super().__init__(name=name)
    self.n_harmonics = n_harmonics
    self.amp_scale_fn = amp_scale_fn
    self.f0_depth = f0_depth
    self.hz_min = hz_min
    self.hz_max = hz_max
    self.sample_rate = sample_rate

    # Layers.
    self.pre_rnn = nn.FcStack(fc_stack_ch, fc_stack_layers)
    self.rnn = nn.Rnn(rnn_ch, rnn_type)
    self.post_rnn = nn.FcStack(fc_stack_ch, fc_stack_layers)

    self.amp_out = tfkl.Dense(1)
    self.hd_out = tfkl.Dense(n_harmonics)
    self.f0_out = tfkl.Dense(f0_depth)

  def call(self, sin_freqs, sin_amps):
    """Converts (sin_freqs, sin_amps) to (f0, amp, hd).

    Args:
      sin_freqs: Sinusoidal frequencies in Hertz, of shape
        [batch, time, n_sinusoids].
      sin_amps: Sinusoidal amplitudes, linear scale, greater than 0, of shape
        [batch, time, n_sinusoids].

    Returns:
      f0: Fundamental frequency in Hertz, of shape [batch, time, 1].
      amp: Amplitude, linear scale, greater than 0, of shape [batch, time, 1].
      hd: Harmonic distribution, linear scale, greater than 0, of shape
        [batch, time, n_harmonics].
    """
    # Scale the inputs.
    nyquist = self.sample_rate / 2.0
    sin_freqs_unit = ddsp.core.hz_to_unit(sin_freqs, hz_min=0.0, hz_max=nyquist)

    # Combine.
    x = tf.concat([sin_freqs_unit, sin_amps], axis=-1)

    # Run it through the network.
    x = self.pre_rnn(x)
    x = self.rnn(x)
    x = self.post_rnn(x)

    harm_amp = self.amp_out(x)
    harm_dist = self.hd_out(x)
    f0 = self.f0_out(x)

    # Output scaling.
    harm_amp = self.amp_scale_fn(harm_amp)
    harm_dist = self.amp_scale_fn(harm_dist)
    f0_hz = ddsp.core.frequencies_softmax(
        f0, depth=self.f0_depth, hz_min=self.hz_min, hz_max=self.hz_max)

    # Filter harmonic distribution for nyquist.
    harm_freqs = ddsp.core.get_harmonic_frequencies(f0_hz, self.n_harmonics)
    harm_dist = ddsp.core.remove_above_nyquist(harm_freqs,
                                               harm_dist,
                                               self.sample_rate)
    harm_dist = ddsp.core.safe_divide(
        harm_dist, tf.reduce_sum(harm_dist, axis=-1, keepdims=True))

    return (harm_amp, harm_dist, f0_hz)

@gin.register
class VideoEncoder(Encoder):
  """Generate latent variables with deep features from a video network."""

  def __init__(self,
               rnn_channels=512,
               rnn_type='gru',
               z_dims=32,
               z_time_steps=250,
               f0_encoder=None,
               other_encoders=None,
               name='mfcc_time_distrbuted_rnn_encoder'):
    super().__init__(f0_encoder=f0_encoder, other_encoders=other_encoders, name=name)
    self.z_time_steps = z_time_steps

    # Layers.
    self.z_norm = nn.Normalize('instance')
    self.rnn = nn.temporal_cnn(rnn_channels, 10)
    self.dense_out = nn.dense(z_dims)
    self.frame_shape = (360, 640, 3)
    self.cv_net = tf.keras.applications.ResNet50V2(include_top=False, weights='imagenet', input_shape=self.frame_shape, pooling=None)
    #TODO(sclarke): Change this for fine tuning
    self.cv_net.trainable = True
    print('Vision network layer count: %i'%len(self.cv_net.layers))
    # for l in self.cv_net.layers:
    #   if not('block7' in l.name or 'top' in l.name):
    #     l.trainable = False
    self.final_layers = tf.keras.Sequential(layers=[
                          tf.keras.layers.GlobalAveragePooling2D(),
                        ])

  def compute_z(self, conditioning):
    batch_flat = tf.reshape(conditioning['frames'], (-1,) + self.frame_shape)
    image_features = self.cv_net(batch_flat)
    condensed = self.final_layers(image_features)
    # rebatched = tf.reshape(condensed, tf.stack([tf.shape(conditioning['frames'])[0], tf.shape(conditioning['frames'])[1], tf.shape(condensed)[-1]], axis=0))
    rebatched = tf.reshape(condensed, [1, 45, 2048])
    # Normalize.
    z = self.z_norm(rebatched[:, :, tf.newaxis, :])[:, :, 0, :]
    # Run an RNN over the latents.
    z = self.rnn(z)
    # Bounce down to compressed z dimensions.
    z = self.dense_out(z)
    #TODO(sclarke):
    return z

@gin.register
class TemporalCnnEncoder(MfccTimeDistributedRnnEncoder):
  """Use MFCCs as latent variables, distribute across timesteps."""

  def __init__(self,
               temporal_cnn_channels=512,
               z_dims=32,
               z_time_steps=250,
               mfcc_time_steps=63,
               window_size=10,
               f0_encoder=None,
               other_encoders=None,
               name='temporal_cnn_encoder'):
    super().__init__(rnn_channels=1, z_dims=z_dims, z_time_steps=mfcc_time_steps, f0_encoder=f0_encoder, other_encoders=other_encoders, name=name)
    self.rnn = nn.temporal_cnn(temporal_cnn_channels, window_size)
    self.z_time_steps = z_time_steps