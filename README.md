# Home Assistant Blueprints

## Hopefully useful to other people than myself.
Some are also referenced on the official Blueprints Exchange forum [https://community.home-assistant.io/c/blueprints-exchange/](https://community.home-assistant.io/c/blueprints-exchange/)

## Directory Structure

The blueprints are organized into the following categories:

- [`/charging`](/charging) - EV charging optimization and load balancing
- [`/lighting`](/lighting) - Motion-based and time-based lighting controls
- [`/buttons`](/buttons) - Button and switch controls (EnOcean, IKEA, Philips)

Each directory contains its own README with detailed information about the blueprints.

## List of blueprints

### Charging

| Blueprint | Description |
|-----------|-------------|
| [EV Charging Time Optimizer](/charging/charge_optimizer.yaml) | Optimize EV charging based on electricity prices and departure time (for Zaptec and Nordpool) |
| [EV Load Balancing](/charging/ev_loadbalancer.yaml) | Avoid overloading the main fuse by adjusting the charge power dynamically based on the current load |

### Lighting

| Blueprint | Description |
|-----------|-------------|
| [Motion Light with Time](/lighting/motion_light_time.yaml) | Simple motion detection that will turn on a light, but also cancel the automation if the light is turned off, ex. by a manual switch |
| [Motion Light with Time and Humidity](/lighting/motion_light_time_humid.yaml) | Motion-activated light that stays on while humidity is above threshold, with configurable time criteria |

### Button Controls

| Blueprint | Description |
|-----------|-------------|
| [ENOcean Light Switch](/buttons/enocean_switch.yaml) | Control two different lights with a 4 button Enocean switch (+/- brightness per light) |
| [ENOcean Light Switch with Color](/buttons/enocean_switch_color.yaml) | Control a 4 button Enocean switch with two buttons controlling light intensity and two buttons cycling between colors |
| [IKEA E2001 Color Control](/buttons/ikea_e2001_color.yaml) | Control for the IKEA E2001 switch with color control functionality |
| [Tap Dial Sonos Control v1](/buttons/philips_tap_dial_sonos.yaml) | Original version of the Sonos controller using entity actions (now deprecated by zigbee2mqtt) |
| [Tap Dial Sonos Control v2](/buttons/philips_tap_dial_sonos_v2.yaml) | Updated version of the Sonos controller using device actions and MQTT with configurable volume adjustment speeds for the dial |