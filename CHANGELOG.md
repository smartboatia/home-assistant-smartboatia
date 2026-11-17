# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First version.

### Added

- Config flow with Smartboatia API key and reauthentication.
- Own MQTT client (not Home Assistant's mqtt integration) that exchanges the key for
  a short-lived token, subscribes read-only to the boat topic and renews the token
  before it expires.
- Decoder for the Smartboatia N-message version 3 (CBOR).
- Device tracker for the boat position.
- Sensors: speed over ground and through water, course over ground, heading, wind
  (ground, true, apparent), water depth, water temperature, atmospheric pressure,
  sea roughness, board voltage, boat state, battery banks, fresh water, fuel and
  black water tanks, engines and transmissions, and a diagnostic last update.
- English and German translations, diagnostics with secrets and position redacted.
- Brand images (`custom_components/smartboatia/brand/`).
