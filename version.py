"""
Dispatcharr version information.
"""
__version__ = '0.31.0'  # Follow semantic versioning (MAJOR.MINOR.PATCH)
__timestamp__ = None    # Set during CI/CD build process
# Not official Dispatcharr: a modified build ("Dispatch More": Channel Switch Overlap, media
# servers, Channel Manager, Stream Check and more). Shown beside the version --
# "v0.31.0 · patched" -- so nobody takes it for a release. __version__ itself is left as it is:
# it goes to providers in the default User-Agent, and the update check compares it with the
# official releases. The installer's build stamps the release in here ("Dispatch More v99").
__build__ = "Dispatch More dev"
