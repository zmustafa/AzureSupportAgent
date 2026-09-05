---
layout: default
title: Keyboard Shortcuts
parent: Reference
nav_order: 4
description: Use global command palette, help, and close-overlay shortcuts.
permalink: /reference/keyboard-shortcuts/
---

# Keyboard shortcuts

| Keys | Action |
| --- | --- |
| **Ctrl + K** (Windows/Linux) or **⌘ + K** (macOS) | Open the Command Palette. |
| **?** | Open Help, including glossary, shortcuts, trust points, and documentation links. |
| **Esc** | Close the active dialog or overlay. |

{% include screenshot.html file="flife-reference-keyboard-shortcuts.png" title="Help — built-in keyboard shortcut reference" caption="Help → Keyboard shortcuts shows the three shipped key/action rows for the Command Palette, Help menu, and closing an overlay. The usage table behind the modal contains dummy model names and figures, not actual provider activity or billing. Opening this reference does not run an application action." %}

## Command Palette behavior

The palette lists only destinations allowed by the active role's effective permissions. It
uses the same route requirements as left navigation and direct-route access checks, including
the current active-role downscope. Switching roles refreshes the identity and immediately
rebuilds the list.

1. Open the palette with **Ctrl + K** or **⌘ + K**, or use the visible **Search** control.
2. Type words from a destination label, group, or keyword. Every space-separated token must
	occur; the search is case-insensitive substring matching rather than an action interpreter.
3. Use **Up Arrow** and **Down Arrow** to select a result, then **Enter** to navigate.
4. Press **Esc** or select the backdrop to close without navigating.

Palette results are routes, even when a label resembles an action such as **Run an assessment**.
Opening a result never submits, approves, applies, tests, or deletes anything. An **admin** badge
is descriptive only; route permission filtering remains authoritative. A manually entered URL
that is not permitted shows **Access not granted** instead of bypassing this filtering.

Shortcuts can be intercepted by the browser, operating system, assistive technology, or an active text editor. Use the visible UI control when a shortcut does not fire.
