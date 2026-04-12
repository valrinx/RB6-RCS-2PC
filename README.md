# RVN — Recoil Control System

> [!CAUTION]
> This program does **not** automatically adjust for your DPI, in-game sensitivity, Windows mouse speed, or any other settings. You must manually tune the values to match your setup.

> [!IMPORTANT]
> The new MAKCU (makcu) will include built-in advanced RCS, which may eventually make this tool obsolete for MAKCU users.  
> **Software Direct mode** (1-PC, no hardware) remains fully supported.

> [!NOTE]
> **RVN — Recoil Control System v5.5**

> **Changes from v5.4:**
> - **FIX:** Trigger mode "LMB Only" now works correctly in hold-to-fire mode.  
>   (Previously, cached button state was polluted by Rapid Fire synthetic clicks, causing stuck states.)
> - Main loop now reads physical LMB the same way as the Rapid Fire worker (more reliable detection).

A **game-agnostic** recoil control script with a clean web-based UI.  
Supports **MAKCU**, **KMBox Net/Pro**, and **Software Direct** (single PC, no hardware).

Works with any game — Rainbow Six Siege, Rust, CS2, Valorant, Apex Legends, etc.

## Features

- **Vertical pull-down** with configurable Delay and Duration
- **Horizontal compensation** (left/right) with Delay and Duration
- **Recoil Curve Editor** — draw custom pull patterns per weapon  
  - **Decay** mode: generates a realistic decaying curve  
  - **Flat** mode: straight line matching the constant value  
  - When the curve ends, it automatically falls back to the constant value
- **Rapid Fire** — auto-clicks LMB at a set interval (great for semi-auto weapons)  
  **→ Now works together with RCS** (recoil compensation still applies during rapid fire)
- **Hip Fire Override** — separate pull-down and horizontal values when firing **without ADS** (RMB not held)
- **Humanization** — Jitter (Gaussian noise) + Exponential Smoothing to make movement less detectable
- **Flexible config system** — save/load configs with free-form tags (`game`, `attach`, `scope`, `grip`, etc.)
- **Tag-based browsing** — filter saved configs by tags or search by name/tag
- **Multiple controller support** — MAKCU, KMBox Net/Pro, or Software Direct
- **Multi-profile support** — separate `.json` files per game or loadout
- **Web UI** — accessible from your phone or a second PC on the same network

## Important Changes in v5.5

- Rapid Fire and Recoil Control can now run **simultaneously**
- Much more reliable physical LMB detection (especially in "LMB Only" + Rapid Fire scenarios)
- Fixed button state issues that previously caused stuck firing states

## Requirements

- Python 3.10+
- One of the following:
  - MAKCU (2-PC hardware)
  - KMBox Net or Pro (2-PC hardware)
  - **Software Direct** (1-PC only — uses Windows SendInput, **may be detected by anti-cheat**)

## Setup

1. Download the release
2. Install dependencies:

```bash
pip install -r requirements.txt
