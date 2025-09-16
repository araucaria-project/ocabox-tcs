"""This is example dumb service, designed to run permanently

Program runner, guider or dome-follower probably should be implemented in this way
"""

import logging
import asyncio
from dataclasses import dataclass

from ocabox_tcs.base_service import BaseServiceConfig
from ocabox_tcs.base_service_ocabox import BaseOCABoxService

from .dumb_complex_svc import service_class, config_class

if __name__ == '__main__':
    service_class.app()
