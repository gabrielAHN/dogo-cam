# Raspberry Pi Wiring

This is the wiring for the current manual-only `dogo-cam` setup.

## GPIO Summary

| Device | Function | GPIO | Physical Pin |
|--------|----------|------|--------------|
| DHT22 | Data | `GPIO4` | `Pin 7` |
| Toggle switch | On/off signal | `GPIO17` | `Pin 11` |
| Tilt servo | PWM signal | `GPIO18` | `Pin 12` |
| Pan servo | PWM signal | `GPIO19` | `Pin 35` |

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

The current switch is a simple on/off switch used by `ky004-control.py`.

| Switch Side | Connect To | Raspberry Pi Pin |
|-------------|------------|------------------|
| Side A | `GPIO17` | `Pin 11` |
| Side B | Ground | `Pin 6` or `Pin 9` |

Behavior:

- switch closed to ground: site stack starts
- switch open: site stack stops

## Servo Wiring

The app uses two MG90S servos:

- `servo1`: tilt
- `servo2`: pan

### Tilt Servo

| Servo Wire | Connect To |
|------------|------------|
| Signal | `GPIO18` / `Pin 12` |
| Power | external `5V` recommended |
| Ground | shared ground with Raspberry Pi |

### Pan Servo

| Servo Wire | Connect To |
|------------|------------|
| Signal | `GPIO19` / `Pin 35` |
| Power | external `5V` recommended |
| Ground | shared ground with Raspberry Pi |

## Power Notes

- Do not rely on weak USB power if both servos move under load.
- Use a power source that can handle the camera and both servos safely.
- Always connect the servo ground and Raspberry Pi ground together.
