# Raspberry Pi Wiring

This is the wiring for the current manual-only `dogo-cam` setup.

## GPIO Summary

| Device | Function | GPIO | Physical Pin |
|--------|----------|------|--------------|
| DHT22 | Data | `GPIO4` | `Pin 7` |
| Toggle switch | On/off signal | `GPIO17` | `Pin 11` |
| Tilt servo | PWM signal | `GPIO18` | `Pin 12` |
| Pan servo | PWM signal | `GPIO19` | `Pin 35` |
| Cooling fan | Power | n/a | `Pin 4` (`5V`) |
| Cooling fan | Ground | n/a | `Pin 6` (`GND`) |
| CSI camera | Ribbon cable | n/a | CSI connector, not GPIO |

## Complete Connection List

| Part | Part Pin | Connect To | Raspberry Pi Pin |
|------|----------|------------|------------------|
| DHT22 | VCC | `3.3V` | `Pin 1` |
| DHT22 | Data | `GPIO4` | `Pin 7` |
| DHT22 | GND | Ground | `Pin 9` |
| Toggle switch | Signal | `GPIO17` | `Pin 11` |
| Toggle switch | VCC | `3.3V` | `Pin 17` |
| Toggle switch | GND | Ground | `Pin 25` |
| Tilt servo | Signal | `GPIO18` | `Pin 12` |
| Tilt servo | V+ | `5V` | `Pin 2` |
| Tilt servo | GND | shared ground | `Pin 14` |
| Pan servo | Signal | `GPIO19` | `Pin 35` |
| Pan servo | V+ | `5V` | `Pin 4` |
| Pan servo | GND | shared ground | `Pin 39` |
| Cooling fan | Red wire | `5V` | `Pin 4` split with pan servo power |
| Cooling fan | Black wire | Ground | `Pin 6` |
| CSI camera | Ribbon cable | CSI camera connector | CSI port between HDMI and audio/video connectors |

## 40-Pin Header View

```text
         3.3V [ 1] [2 ] 5V
        GPIO2 [ 3] [4 ] 5V
        GPIO3 [ 5] [6 ] GND
        GPIO4 [ 7] [8 ] GPIO14
          GND [ 9] [10] GPIO15
       GPIO17 [11] [12] GPIO18
       GPIO27 [13] [14] GND
       GPIO22 [15] [16] GPIO23
         3.3V [17] [18] GPIO24
       GPIO10 [19] [20] GND
        GPIO9 [21] [22] GPIO25
       GPIO11 [23] [24] GPIO8
          GND [25] [26] GPIO7
        GPIO0 [27] [28] GPIO1
        GPIO5 [29] [30] GND
        GPIO6 [31] [32] GPIO12
       GPIO13 [33] [34] GND
       GPIO19 [35] [36] GPIO16
       GPIO26 [37] [38] GPIO20
          GND [39] [40] GPIO21
```

## DHT22

| DHT22 Pin | Connect To | Raspberry Pi Pin |
|-----------|------------|------------------|
| VCC | 3.3V | `Pin 1` |
| Data | `GPIO4` | `Pin 7` |
| GND | Ground | `Pin 9` |

Use a `4.7kΩ` to `10kΩ` pull-up resistor between DHT22 `VCC` and `Data`.

## Toggle Switch

The current switch is documented here as a 3-pin switch module used by `ky004-control.py`.

| Switch Pin | Connect To | Raspberry Pi Pin |
|------------|------------|------------------|
| Signal | `GPIO17` | `Pin 11` |
| VCC | `3.3V` | `Pin 17` |
| GND | Ground | `Pin 25` |

Behavior:

- signal low: site stack starts
- signal high: site stack stops

## Servo Wiring

The app uses two MG90S servos:

- `servo1`: tilt
- `servo2`: pan

### Tilt Servo

| Servo Wire | Connect To | Raspberry Pi Pin |
|------------|------------|------------------|
| Signal | `GPIO18` | `Pin 12` |
| Power | `5V` | `Pin 2` |
| Ground | Ground | `Pin 14` |

### Pan Servo

| Servo Wire | Connect To | Raspberry Pi Pin |
|------------|------------|------------------|
| Signal | `GPIO19` | `Pin 35` |
| Power | `5V` | `Pin 4` |
| Ground | Ground | `Pin 39` |

## Cooling Fan

Use a simple always-on two-wire fan unless you have a separate fan controller.

| Fan Wire | Connect To | Raspberry Pi Pin |
|----------|------------|------------------|
| Red | `5V` | `Pin 4` split with pan servo power |
| Black | Ground | `Pin 6` |

If your fan is a `3.3V` fan, connect power to `Pin 1` instead of `Pin 4`.

## CSI Camera

The camera does not use the 40-pin GPIO header.

| Camera Connection | Connect To |
|-------------------|------------|
| Ribbon cable | Raspberry Pi CSI camera connector |

## Power Notes

- Do not rely on weak USB power if both servos move under load.
- Use a Raspberry Pi power supply that can handle the camera, both servos, and the fan safely.
- Always connect the servo ground and Raspberry Pi ground together.
- A `5V` fan adds constant load to the Pi power rail, so use a stable Raspberry Pi power supply.
- The Pi only has two `5V` pins, so the cooling fan is plugged into the Pi by splitting `Pin 4` with the pan servo power lead.
