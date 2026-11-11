"""Runs first: point every test at a throw-away data folder, so no test can write the real settings.json.

(tello_net reads TELLO_CONNECT_DATA when it is first imported, and test modules import it in file order.)
"""
import os
import tempfile

os.environ.setdefault("TELLO_CONNECT_DATA", tempfile.mkdtemp(prefix="tello-test-data-"))
