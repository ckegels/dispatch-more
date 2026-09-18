"""
Dispatcharr version information.
"""
__version__ = '0.31.0'  # Follow semantic versioning (MAJOR.MINOR.PATCH)
__timestamp__ = None    # Set during CI/CD build process
# Not official Dispatcharr: a modified build (Channel Switch Overlap, media servers, Channel
# Manager, Stream Check and more). Shown after the version -- v0.31.0+mod -- so nobody takes
# it for a release. __version__ itself is left as it is: it goes to providers in the default
# User-Agent, and the update check compares it with the official releases.
__build__ = "mod"
