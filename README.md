# Ulanzi K6500 BLE Light (Home Assistant)

Custom integration for controlling the **Ulanzi K6500** monitor light over **Bluetooth Low Energy** from Home Assistant.

## Requirements

- Home Assistant **2023.8** or newer (config entry platform loading).
- The **Bluetooth** integration enabled and a working BLE adapter.
- The device **MAC address** (from your OS, router, or a BLE scanner).

## Installation (HACS)

1. In HACS, open **Integrations** → menu (⋮) → **Custom repositories**.
2. Add this repository URL, category: **Integration**.
3. Install **Ulanzi K6500 BLE Light**.
4. Restart Home Assistant.

## Manual installation

Copy the `custom_components/ulanzi` folder into your Home Assistant `config/custom_components/` directory and restart.

## Configuration (UI)

1. **Settings** → **Devices & services** → **Add integration**.
2. Search for **Ulanzi K6500 BLE Light** (or **Ulanzi**).
3. Enter the Bluetooth **MAC address** (e.g. `AA:BB:CC:DD:EE:FF` or `AA-BB-CC-DD-EE-FF`) and an optional **name**.

The same MAC cannot be added twice.

## Behaviour

The integration uses short BLE connections and **optimistic** state: after a successful write, Home Assistant assumes the command reached the lamp. The device does not report state back over this protocol.

## Before publishing

Replace `YOUR_USERNAME` in `manifest.json` (`documentation` and `issue_tracker`) with your GitHub username or organization.

## License

MIT (or your choice — update this section when publishing).
