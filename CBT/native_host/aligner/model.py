import torch
import torchaudio


class AlignmentModel:
    """
    Singleton wrapper around the MMS Forced Alignment model.

    Loads the model only once during the application lifetime.
    """

    _instance = None

    def __new__(cls):

        if cls._instance is None:

            cls._instance = super().__new__(cls)

            cls._instance._initialize()

        return cls._instance

    def _initialize(self):

        print("=" * 60)
        print("Loading MMS Forced Alignment Model...")
        print("=" * 60)

        # --------------------------------------------
        # Device selection
        #
        # Forced alignment is a full acoustic-model forward pass, so
        # it benefits significantly from GPU acceleration when a
        # CUDA-capable GPU is present. Falls back to CPU automatically
        # otherwise — no code path change needed on machines without
        # a GPU, which matters for this project's offline/air-gapped
        # deployment target.
        # --------------------------------------------

        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        )

        print(f"Selected Device : {self.device}")

        if self.device.type == "cuda":
            print(f"GPU             : {torch.cuda.get_device_name(self.device)}")

        # Official torchaudio bundle
        self.bundle = torchaudio.pipelines.MMS_FA

        print("Bundle Loaded")

        # Sample rate expected by the model
        self.sample_rate = self.bundle.sample_rate

        print(f"Expected Sample Rate : {self.sample_rate}")

        # Character labels
        self.labels = self.bundle.get_labels()

        print(f"Labels : {len(self.labels)}")

        # Load pretrained model
        self.model = self.bundle.get_model()

        self.model.to(self.device)

        self.model.eval()

        print("Model Loaded Successfully")
        print("=" * 60)

    # -------------------------------------------------
    # Public Methods
    # -------------------------------------------------

    def get_model(self):
        return self.model

    def get_bundle(self):
        return self.bundle

    def get_labels(self):
        return self.labels

    def get_sample_rate(self):
        return self.sample_rate

    def get_device(self):
        return self.device


# Global singleton
alignment_model = AlignmentModel()  