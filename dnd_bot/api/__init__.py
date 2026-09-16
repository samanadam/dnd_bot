"""HTTP API for the portal.

Nothing under here may be imported by the recorder: the API drives
SessionManager and MusicManager, never the reverse, so a web failure can never
reach a live recording.
"""
