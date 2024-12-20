from pathlib import Path

import logging
import sys
import yaml

_logger = logging.getLogger("config")


class ServicesConfigFile(dict):
    source: Path | None = None

    def load_config(self, config_file: str = "../../config/services.yaml"):

        path = Path(config_file)
        if not path.is_absolute():
            path = Path(__file__).parent / path   # Load config from relative path e.g. ../../config
        try:
            with open(path) as f:
                config = yaml.safe_load(f)
                self.update(config)
                self.source = path
        except Exception as e:
            _logger.error(f"Failed to load config: {str(e)}")
            sys.exit(1)
