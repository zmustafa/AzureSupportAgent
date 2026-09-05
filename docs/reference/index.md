---
layout: default
title: Reference
nav_order: 23
description: Find product permissions, glossary, troubleshooting routes, and keyboard shortcuts.
permalink: /reference/
has_children: true
---

# Reference

- [Permissions]({{ site.baseurl }}/reference/permissions/) — capability keys and built-in role intent.
- [Glossary]({{ site.baseurl }}/reference/glossary/) — canonical concepts pointer.
- [Troubleshooting index]({{ site.baseurl }}/reference/troubleshooting/) — symptom-to-guide map.
- [Keyboard shortcuts]({{ site.baseurl }}/reference/keyboard-shortcuts/) — global navigation keys.
- [Visual tour]({{ site.baseurl }}/reference/visual-tour/) — nine screenshot highlights and twenty workflow groups linking into the 137-capture collection.

The live role editor and in-app Help menu reflect the running build and are authoritative when they differ from a static release page.

## Read a permission reference against the UI

Use the **Roles** view to match a capability in the permissions guide to a product role. This is application authorization, not the Azure or Graph access carried by a connection.

{% include screenshot.html file="admin-access-built-in-roles.png" title="Reference in practice — exact capabilities in the built-in role catalog" caption="The role catalog shows read, write, run, manage, and approval capabilities as separate keys. Compare the key required by a feature with the active permission set, not just its role name. This local built-in catalog is not a Help or keyboard-shortcut overlay; no assignments were changed." %}
