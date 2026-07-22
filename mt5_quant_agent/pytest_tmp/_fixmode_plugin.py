import pathlib
_orig = pathlib.Path.mkdir
def _mkdir(self, mode=0o777, parents=False, exist_ok=False):
    if mode == 0o700: mode = 0o777
    return _orig(self, mode=mode, parents=parents, exist_ok=exist_ok)
pathlib.Path.mkdir = _mkdir
