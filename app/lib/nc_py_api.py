"""Minimal stand-in for nc_py_api, for the capture service.

The signaling client is taken from the transcription app, where Nextcloud
supplies the backend URL and the HPB settings. None of that exists here: the
configuration comes from the environment.

Everything else raises: a silent stand-in would suggest that translation or the
AppAPI are available.
"""
import os


class NextcloudException(Exception):
	pass


class _Options:
	# get_ssl_context() checks this before falling back to the default context.
	nc_cert = None


class _AppCfg:
	def __init__(self):
		self.endpoint = os.environ.get("CEFFIO_NEXTCLOUD_URL", "")
		self.options = _Options()


class NextcloudApp:
	def __init__(self, *args, **kwargs):
		self.app_cfg = _AppCfg()

	def ocs(self, *args, **kwargs):
		raise RuntimeError(
			"nc_py_api is not available in the capture service: "
			"configuration comes from the environment"
		)


class AsyncNextcloudApp(NextcloudApp):
	pass
