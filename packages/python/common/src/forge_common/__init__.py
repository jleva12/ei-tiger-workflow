"""Code the Forge Python apps and packages share.

Each subpackage brings its own dependencies as an extra, so a codebase installs
only what it imports: ``forge_common.adk`` needs ``forge-common[adk]``.
"""
