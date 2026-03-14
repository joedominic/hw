# Suppress requests dependency version warning (urllib3/charset_normalizer versions work at runtime)
import warnings
warnings.filterwarnings(
    "ignore",
    message=".*doesn't match a supported version.*",
    module="requests",
)
