# Smartboatia for Home Assistant

**Your boat knows more than it shows you. Soon, your Home Assistant will too.**

This integration brings your boat into your smart home: position, batteries, wind and alarms from [Smartboatia](https://www.smartboatia.com/en/), right next to the lights, the heating and everything else you already run in Home Assistant.

> **Status:** first version 0.1.0, prepared for release. It needs the Smartboatia cloud
> API key feature, which is being rolled out; see [Requirements](#requirements).

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=smartboatia&repository=home-assistant-smartboatia&category=integration)

## Why your boat belongs in your smart home

You know whether the garage door is closed. But is your boat fine? Is it still where you left it, are the batteries holding up, is the wind picking up at the anchorage? With this integration your boat becomes part of your home automation:

- your boat's status on your Home Assistant dashboard
- automations around your boat, like a notification on your phone or a light that turns red when the battery runs low or the wind gets strong
- boat data combined with everything else in your home, from the weather station in the garden to the calendar of your next trip

## What is Smartboatia?

Smartboatia makes boats smarter, for sail and motor boats alike. The Smartboatia Hub on board, the app and the cloud work together and turn the data your boat already produces into answers instead of numbers. Built by sailors in Travemünde, Germany.

- 📔 **Logbook that writes itself.** Casting off, anchoring, mooring, engine hours and manoeuvres are logged automatically. Add notes, photos and crew whenever you like.
- ⚓ **Boat monitoring, on board and ashore.** Anchor watch, batteries and position at a glance, with alarms that run on board even when your phone is off.
- 🌬️ **Weather and environment.** Your boat measures for itself: real trends in wind and air pressure, exactly where you are.
- 🧭 **Plotter and navigation.** Your boat's data and charts on every device.
- 📱 **Community and sharing.** Share your trips with crew, family and friends, and see where your boat buddies are.
- 🏆 **Sailing performance.** Polars from your own data, trim tips and manoeuvre analysis, for sailors.

## Open by design

Your data belongs to you. Smartboatia uses open interfaces and connects to what you already have instead of locking you in. This integration is part of that promise.

And every mile with Smartboatia helps the oceans: environmental data from boats flows into ocean research with partners such as GEOMAR, AWI and the German Weather Service (DWD).

👉 [Discover Smartboatia](https://www.smartboatia.com/en/) · [Try it for 30 days](https://www.smartboatia.com/en/30-day-trial/)

## Requirements

- A Smartboatia Hub that sends data to the cloud, with an active Smartboatia
  subscription on the boat.
- You are an owner of the boat in your Smartboatia account.
- Home Assistant 2025.5 or newer. This minimum comes from checking the Home Assistant
  sources: the `measurement_angle` state class first appears in 2025.4.0 and the
  `L/h` flow rate unit in 2025.5.0. The test suite itself is run against 2026.2.3 only;
  2025.5 was not run.

## Installation

### Home Assistant 2026.11 and newer: Marketplace

Home Assistant 2026.11 has the community store built in as the **Marketplace**
(Settings, Marketplace). Until this integration is in the default catalog, add it as
a custom repository:

1. Click the **Add to Home Assistant** button above, or open Settings, Marketplace,
   **Add from link** and enter `smartboatia/home-assistant-smartboatia`.
   (Adding custom repositories needs a GitHub connection through device sign-in.)
2. Install **Smartboatia**. Setup starts without a restart.

### Older versions: HACS

1. Click the **Add to Home Assistant** button above, or in HACS open the three-dot
   menu, **Custom repositories**, enter
   `https://github.com/smartboatia/home-assistant-smartboatia`, category
   **Integration**.
2. Install **Smartboatia** and restart Home Assistant.

### Manual

Copy `custom_components/smartboatia` into the `custom_components` folder of your
Home Assistant configuration and restart.

## Get an API key

Create the key in your Smartboatia account (the exact menu path is still to be
confirmed while the account feature is being finished). The key
starts with `sbk_`. It is shown once, so copy it right away. Keys can be revoked in
the account at any time; Home Assistant then asks for a new key (reauthentication).
Treat the key like a password.

## Set up

Settings, Devices & services, Add integration, **Smartboatia**, paste the API
key. One entry is one boat; add more entries with other keys for more boats.

## How it works

The key is exchanged at the Smartboatia cloud for a short-lived MQTT token (valid
one hour). The integration connects to the cloud broker over TLS, subscribes to the
one topic of your boat and decodes the status message (N-message version 3). The
last message is retained, so values appear right after connecting; a new one
arrives about every minute. Shortly before the token expires, a new one is fetched
and the connection is renewed automatically.

## Entities

One device per boat. Entities appear when the value is first seen. A value that is
missing from a message (sensor off, data too old) makes the entity
**unavailable**, never zero. If no message arrives for ten minutes all values
become unavailable. The broker replays the last message when Home Assistant
connects; if that message is older than ten minutes (a boat that was offline) it
is not shown as live. A diagnostic sensor **Last update** always shows the time of
the latest message, so you can see how old the data is. (A Hub whose clock is
hours behind real time would therefore always show its data as old.)

| Entity | Unit |
|---|---|
| Position (device tracker, GPS) | |
| Last update (diagnostic timestamp) | |
| Speed over ground | kn |
| Speed through water | kn |
| Course over ground (true, magnetic if sent) | ° |
| Heading | ° |
| Wind speed and direction (ground / true) or angle (apparent) | kn, ° |
| Water depth | m |
| Water temperature | °C |
| Atmospheric pressure | hPa |
| Sea roughness | no unit |
| Board voltage (Hub supply) | V |
| Boat state (dwelling, docked, anchored, sailing, motoring, ...) | |
| Battery bank N: voltage, current, temperature, charge, health | V, A, °C, % |
| Fresh water, fuel and black water tank N: level | % |
| Engine N: speed, boost pressure, tilt/trim, coolant and oil temperature, oil and fuel and coolant pressure, alternator voltage, fuel rate, hours, load, torque, status 1 and 2 | rpm, bar, °C, V, L/h, h, % |
| Transmission N: gear, oil pressure, oil temperature | bar, °C |

`N` is the NMEA 2000 instance number as the Hub reports it (starting at 0). Which
entities you get depends on what is on your boat's bus. Entities for heading and
speed through water only appear if the Hub sends them. Alarms are not part of
this version.

## Limits

- The cloud allows 20 token exchanges per key per hour. The integration needs
  about one per hour, never makes two within four minutes and backs off on every
  error (also when the broker refuses or drops the connection), so it stays at or
  below 15 per hour even when everything fails. Only several Home Assistant
  instances sharing one key, or repeated restarts, can reach the limit.
- Data is at most as fresh as the Hub's message interval (about 60 seconds).
- Only N-message version 3 is understood; other formats are ignored.
- If the boat has no active subscription, or the person who created the key is no
  longer an owner, the integration cannot connect (it asks for a new key in the
  second case).

## Troubleshooting

- **"The API key is invalid, revoked or expired"**: create a new key in your
  Smartboatia account and enter it again (if a working key is rejected later,
  Home Assistant asks for a new one: re-authentication).
- **"No longer an owner of the boat"**: the key only works while the person who
  created it is an owner of the boat. Create a new key as an owner.
- **"No active subscription"**: the boat needs an active Smartboatia subscription.
  The integration keeps retrying slowly and starts working once there is one.
- **Entities stay unavailable**: check the **Last update** sensor. If it is old,
  the Hub is not sending data (offline, no internet). The data is only shown as live
  if the message is newer than ten minutes.
- **Entities missing**: entities for batteries, tanks, engines and so on appear
  when the Hub reports them for the first time, within about a minute.
- **Debug logs**: add `custom_components.smartboatia: debug` to the `logger`
  configuration. The API key and the token are never written to the log.

## Privacy

The API key and the MQTT token are never written to the log. Diagnostics redact
them, together with the boat identifier.

## Support

- Questions about Smartboatia, the app or your account: [Smartboatia help portal](https://smartboatia.atlassian.net/servicedesk/customer/portals) or support@smartboatia.com
- Bugs and feature requests for this integration: please open an [issue](../../issues)
- Security issues: please do not open a public issue, see [SECURITY.md](SECURITY.md)

## Contributing

Issues and pull requests are welcome. Please follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

---

**Sail smarter. Sail together. Protect the oceans.**
[Smartboatia GmbH](https://www.smartboatia.com/en/), 23570 Lübeck, Germany
