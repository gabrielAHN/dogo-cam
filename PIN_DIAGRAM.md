# Raspberry Pi Pin Diagram - Complete Hardware Setup

## Raspberry Pi 3 GPIO Pinout (40-pin header)

```
         3.3V [ 1] [2 ] 5V
        GPIO2 [ 3] [4 ] 5V
        GPIO3 [ 5] [6 ] GND         ← Button G (ground)
        GPIO4 [ 7] [8 ] GPIO14
          GND [ 9] [10] GPIO15
       GPIO17 [11] [12] GPIO18      ← LED Control (Option 2)
       GPIO27 [13] [14] GND
       GPIO22 [15] [16] GPIO23
         3.3V [17] [18] GPIO24      ← Button V (power)
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

## Pin Usage Summary

### DHT22 Temperature/Humidity Sensor

| Sensor Pin | Connected To | Physical Pin | GPIO/Function |
|------------|--------------|--------------|---------------|
| VCC (Red) | 3.3V Power | **Pin 1** | 3.3V |
| Data (Yellow) | Data Signal | **Pin 7** | GPIO4 |
| GND (Black) | Ground | **Pin 9** | GND |

**Pull-up Resistor**: 4.7kΩ-10kΩ between VCC and Data pins

### G-V-S Button Module - Option 1 (Simple Mode)

| Button Pin | Connected To | Physical Pin | GPIO/Function |
|------------|--------------|--------------|---------------|
| G (Ground) | Ground | **Pin 6** | GND |
| V (Power) | 3.3V Power | **Pin 17** | 3.3V |
| S (Signal) | Button Input | **Pin 5** | GPIO3 |

**LED Behavior**: Always ON when Pi is powered

### G-V-S Button Module - Option 2 (Full LED Control)

| Connection | Connected To | Physical Pin | GPIO/Function |
|------------|--------------|--------------|---------------|
| G (LED GND) | Ground | **Pin 6** | GND |
| V (LED Power) | 3.3V Power | **Pin 17** | 3.3V |
| S (LED Control) | LED Signal | **Pin 11** | GPIO17 |
| Button Switch 1* | Button Input | **Pin 5** | GPIO3 |
| Button Switch 2* | Ground | **Pin 6** | GND |

*Internal button switch contacts (requires opening module or testing with multimeter)

**LED Behavior**: Blinks based on system state (slow/medium/fast/solid/off)

## Visual Pin Diagram - Option 1 (Simple)

```
Raspberry Pi GPIO Header
┌─────────────────────────────────────────┐
│                                         │
│  [1]  3.3V ●━━━━━━━━━━━━━━━━━━━━━┓    │  DHT22 VCC (Red wire)
│                                   ┃    │
│  [2]  5V                          ┃    │
│  [3]  GPIO2                       ┃    │
│  [4]  5V                          ┃    │
│  [5]  GPIO3 ●━━━━━━━━━━━━━━━┓    ┃    │  Button S (Signal)
│                             ┃    ┃    │
│  [6]  GND   ●━━━━━━━━━┓    ┃    ┃    │  Button G (Ground)
│                       ┃    ┃    ┃    │
│  [7]  GPIO4 ●━━━━┓   ┃    ┃    ┃    │  DHT22 Data (Yellow wire)
│                  ┃   ┃    ┃    ┃    │
│  [8]  GPIO14     ┃   ┃    ┃    ┃    │
│  [9]  GND   ●━━━━┫   ┃    ┃    ┃    │  DHT22 GND (Black wire)
│                  ┃   ┃    ┃    ┃    │
│  [10] GPIO15     ┃   ┃    ┃    ┃    │
│  [11] GPIO17     ┃   ┃    ┃    ┃    │
│  [12] GPIO18     ┃   ┃    ┃    ┃    │
│  [13] GPIO27     ┃   ┃    ┃    ┃    │
│  [14] GND        ┃   ┃    ┃    ┃    │
│  [15] GPIO22     ┃   ┃    ┃    ┃    │
│  [16] GPIO23     ┃   ┃    ┃    ┃    │
│  [17] 3.3V  ●━━━━┫━━━┫━━━━┫━━━━┫━┓  │  Button V (Power)
│                  ┃   ┃    ┃    ┃ ┃  │
│       ...        ┃   ┃    ┃    ┃ ┃  │
└─────────────────────────────────────────┘
                   ┃   ┃    ┃    ┃ ┃
                   ┃   ┃    ┃    ┃ ┃
      To DHT22: ━━━┻━━━┻━━━━┛    ┃ ┃
      (GND, Data, VCC)            ┃ ┃
                                  ┃ ┃
      To Button: ━━━━━━━━━━━━━━━━┻━┻
      (GND, Signal, Power)
```

## Visual Pin Diagram - Option 2 (Full LED Control)

```
Raspberry Pi GPIO Header
┌─────────────────────────────────────────┐
│                                         │
│  [1]  3.3V ●━━━━━━━━━━━━━━━━━━━━━┓    │  DHT22 VCC (Red wire)
│                                   ┃    │
│  [2]  5V                          ┃    │
│  [3]  GPIO2                       ┃    │
│  [4]  5V                          ┃    │
│  [5]  GPIO3 ●━━━━━━━━━━━━━━━┓    ┃    │  Button Switch Contact 1
│                             ┃    ┃    │
│  [6]  GND   ●━━━━━━━━━┓━━━━┫    ┃    │  Button G + Switch Contact 2
│                       ┃    ┃    ┃    │
│  [7]  GPIO4 ●━━━━┓   ┃    ┃    ┃    │  DHT22 Data (Yellow wire)
│                  ┃   ┃    ┃    ┃    │
│  [8]  GPIO14     ┃   ┃    ┃    ┃    │
│  [9]  GND   ●━━━━┫   ┃    ┃    ┃    │  DHT22 GND (Black wire)
│                  ┃   ┃    ┃    ┃    │
│  [10] GPIO15     ┃   ┃    ┃    ┃    │
│  [11] GPIO17 ●━━━┫━━━┫━━━━┫━━━━┫━┓  │  Button S (LED Control)
│                  ┃   ┃    ┃    ┃ ┃  │
│  [12] GPIO18     ┃   ┃    ┃    ┃ ┃  │
│  [13] GPIO27     ┃   ┃    ┃    ┃ ┃  │
│  [14] GND        ┃   ┃    ┃    ┃ ┃  │
│  [15] GPIO22     ┃   ┃    ┃    ┃ ┃  │
│  [16] GPIO23     ┃   ┃    ┃    ┃ ┃  │
│  [17] 3.3V  ●━━━━┫━━━┫━━━━┫━━━━┫━┫  │  Button V (LED Power)
│                  ┃   ┃    ┃    ┃ ┃  │
│       ...        ┃   ┃    ┃    ┃ ┃  │
└─────────────────────────────────────────┘
                   ┃   ┃    ┃    ┃ ┃
      To DHT22: ━━━┻━━━┻━━━━┛    ┃ ┃
      (GND, Data, VCC)            ┃ ┃
                                  ┃ ┃
      To Button LED: ━━━━━━━━━━━━┻━┻
      (GND, GPIO17, Power)

      To Button Switch: ━━━━━━━━━━┻
      (Contact 1=GPIO3, Contact 2=GND)
```

## Quick Reference Table - All Pins Used

| Physical Pin | GPIO/Function | Connected To (Option 1) | Connected To (Option 2) |
|--------------|---------------|-------------------------|-------------------------|
| Pin 1 | 3.3V | DHT22 VCC | DHT22 VCC |
| Pin 5 | GPIO3 | Button S (signal) | Button Switch Contact 1 |
| Pin 6 | GND | Button G (ground) | Button G + Switch Contact 2 |
| Pin 7 | GPIO4 | DHT22 Data | DHT22 Data |
| Pin 9 | GND | DHT22 GND | DHT22 GND |
| Pin 11 | GPIO17 | Not used | Button S (LED control) |
| Pin 17 | 3.3V | Button V (power) | Button V (LED power) |

## Important Notes

1. **No Pin Conflicts**: Each component uses unique pins - no sharing except ground
2. **3.3V Pins**:
   - Pin 1 used for DHT22 sensor
   - Pin 17 used for button LED power
3. **Ground Pins**: Multiple devices can share ground (Pin 6, Pin 9)
4. **GPIO3 Wake Feature**: GPIO3 (Pin 5) can wake the Pi from powered-off state
5. **Pull-up Resistor**: Required between DHT22 VCC and Data pins (4.7kΩ-10kΩ)
