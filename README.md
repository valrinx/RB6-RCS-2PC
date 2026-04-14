# RVN — Recoil Control System

> [!CAUTION]
> This program does not automatically account for your DPI, sensitivity, Windows mouse speed, or any other settings. You will need to tune the values manually to match your setup.

> [!IMPORTANT]
> The MAKXD (new makcu) will have built-in advanced RCS, which may eventually make this tool obsolete for makcu users. Software Direct mode (1-PC, no hardware) is still fully supported.

> [!NOTE]
> **RVN — Recoil Control System  v7.0**
>
> **Changes from v6.0:**
> - **FIX:** Weapon Slot dropdowns now correctly show saved gun configs
    (buildWsGrid was called before fetchConfigs finished loading)
> **NEW:** Per-slot Rapid Fire — each weapon slot can have its own RF setting
    Slot 1: RF OFF, Slot 2: RF ON 80ms, etc.
    "inherit" = use global RF setting (same as before)
    
A game-agnostic recoil control script with a web UI. Supports MAKCU, KMBox, and Software Direct (no hardware, 1-PC).

Works with any game — R6, Rust, CS2, Valorant, or anything else.

## Features

- **Vertical pull-down** with Delay and Duration timing
- **Horizontal compensation** with Delay and Duration timing
- **Recoil Curve editor** — draw a custom pull pattern per gun
  - **Decay** — generates a realistic decaying curve from the constant value
  - **Flat** — sets the curve to a straight line matching the constant value
  - When the curve runs out, falls back to the constant value automatically
- **Rapid Fire** — auto-clicks LMB at a set interval (for semi-auto weapons)
- **Hip Fire override** — separate pull-down and horizontal values when not ADS
- **Humanization** — Jitter + Exponential Smoothing to reduce pattern detection
- **Game-agnostic config system** — save configs with free-form tags (`game`, `attach`, `scope`, etc.)
- **Tag-based browsing** — filter saved configs by any tag; search by name or tag
- **Multiple controller support** — MAKCU, KMBox Net/Pro, or Software Direct (1-PC)
- **Multi-profile system** — separate `.json` files per game or loadout
- Web UI accessible from phone or second PC on the same network

## Requirements

- Python 3.10+
- One of:
  - A MAKCU (2-PC hardware)
  - A KMBox Net or Pro (2-PC hardware)
  - Nothing — Software Direct mode works on a single PC (no Anti-Cheat bypass)

## Setup

Download the release, then install dependencies:

```bash
pip install -r requirements.txt
```

If accessing from another device on your network, allow port 8000 through Windows Firewall (run in PowerShell as admin):

```powershell
New-NetFirewallRule -DisplayName "RVN Port 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000
```

## Usage

Double-click `rvn_v5.py`, or run in terminal:

```bash
python rvn_v5.py
```

The console will print the URLs to open:

```
  Local  : http://localhost:8000
  Network: http://192.168.x.x:8000
```

Open that URL in any browser — works from your phone too.

## Controls

| Setting | Description |
|---|---|
| **Vertical (Pull-down)** | How much the mouse pulls down per tick while firing |
| **Vertical Delay** | How long after firing starts before vertical kicks in (ms) |
| **Vertical Duration** | How long vertical lasts — 0 = forever |
| **Horizontal** | Left/right compensation (negative = left, positive = right) |
| **Horizontal Delay** | How long before horizontal kicks in (ms) |
| **Horizontal Duration** | How long horizontal lasts — 0 = forever |
| **Recoil Curve** | Draw a custom pull pattern — overrides the constant value while the curve lasts, then falls back to the constant |
| **Rapid Fire** | Auto-click LMB at a fixed interval — for semi-auto weapons; disables RCS pull-down while active |
| **Hip Fire** | Separate pull-down and horizontal values used when firing without ADS (no RMB held) |
| **Jitter** | Gaussian noise per tick to randomize movement slightly |
| **Smooth** | Exponential smoothing — higher = softer, more natural movement |
| **Toggle key** | M4, M5, or Middle Mouse — toggles recoil on/off in-game |
| **Trigger mode** | LMB only, or LMB + RMB (fire + ADS simultaneously) |

## Saving Configs

Configs are saved per profile (`.json` file). Each config has:

- **Name** — any name you want (e.g. `AK47`, `MP5K Comp`)
- **Tags** — optional key:value pairs for filtering (e.g. `game:Rust`, `attach:Compensator`)

The browse dropdown shows only the gun name and pull-down value for a clean look. Hover over an entry to see its full tags, or use the search bar to filter by name or tag.

Example tags for different games:

```
# Rust
game:Rust   attach:Comp   scope:2x

# R6
game:R6   op:Ash   attach:Flash

# CS2
game:CS2   attach:Silencer
```

Configs are stored in the `configs/` folder as `.json` files. Create separate profiles per game from the Settings → Profile panel.

## Accessing from Another Device

Both devices must be on the same network (same Wi-Fi or LAN). Use the Network IP shown in the console, on port 8000.

## Contributors

- secretlay3r — code cleanup
- blainsage — firewall tip (port 8000)
