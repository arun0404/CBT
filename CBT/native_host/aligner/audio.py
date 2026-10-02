import soundfile as sf
import torch
import torchaudio

from aligner.model import alignment_model


class AudioProcessor:

    def __init__(self):
        self.target_sample_rate = alignment_model.get_sample_rate()

    def load(self, wav_path):

        waveform, sample_rate = sf.read(wav_path)

        print("=" * 60)
        print("Loading Audio")
        print("=" * 60)

        print(f"Original Sample Rate : {sample_rate}")

        # Convert numpy -> torch
        waveform = torch.tensor(waveform, dtype=torch.float32)

        # Mono conversion
        if waveform.ndim == 2:

            waveform = waveform.mean(dim=1)

            print("Converted Stereo -> Mono")

        # Shape -> [1, samples]
        waveform = waveform.unsqueeze(0)

        # Resample if necessary
        if sample_rate != self.target_sample_rate:

            print(f"Resampling {sample_rate} -> {self.target_sample_rate}")

            resampler = torchaudio.transforms.Resample(
                sample_rate,
                self.target_sample_rate
            )

            waveform = resampler(waveform)

            sample_rate = self.target_sample_rate

        # Normalize
        waveform = waveform / waveform.abs().max()

        print(f"Final Shape : {waveform.shape}")
        print(f"Final Sample Rate : {sample_rate}")

        print("=" * 60)

        return waveform, sample_rate


audio_processor = AudioProcessor()