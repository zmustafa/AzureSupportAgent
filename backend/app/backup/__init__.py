"""Whole-tenant Backup & Restore.

Exports a tenant's configuration + operational data as a portable, secret-free JSON
manifest wrapped in a ZIP archive for download. The archive can also include an
export-only nested chats HTML ZIP. Restores use the manifest content on the same (or a
rebuilt) instance. See ``registry`` for the manifest format and the section registry.
"""
