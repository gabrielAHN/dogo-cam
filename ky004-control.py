#!/usr/bin/env python3
"""
KY-004 Button Control for Dogo-Cam
Simple shutdown button - services auto-start on boot

Button behavior:
- When Pi is on: Press to shutdown
- When Pi is off: Press to wake via GPIO3 hardware feature
"""
import lgpio
import time
import os
import logging

BUTTON_PIN = 3
DEBOUNCE_TIME = 0.1
SHUTDOWN_STATE_FILE = "/tmp/shutdown_pending"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def shutdown_system():
    """Initiate system shutdown"""
    logger.info("Button pressed - initiating system shutdown...")
    with open(SHUTDOWN_STATE_FILE, "w") as f:
        f.write("1")
    time.sleep(2)  # Give Flask app time to detect shutdown and stop camera
    os.system("sudo shutdown -h now")


def main():
    # Clean up shutdown state file on startup
    if os.path.exists(SHUTDOWN_STATE_FILE):
        os.remove(SHUTDOWN_STATE_FILE)

    # Initialize GPIO
    h = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(h, BUTTON_PIN, lgpio.SET_PULL_UP)

    logger.info(f"Power button monitoring on GPIO{BUTTON_PIN} - press to shutdown")

    try:
        while True:
            # Check for button press (reads 0 when pressed due to pull-up logic)
            if lgpio.gpio_read(h, BUTTON_PIN) == 0:
                time.sleep(DEBOUNCE_TIME)
                if lgpio.gpio_read(h, BUTTON_PIN) == 0:  # Confirm press after debounce
                    logger.info("Button pressed!")
                    lgpio.gpiochip_close(h)
                    shutdown_system()
                    break
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.info("\nStopped by user")
    finally:
        lgpio.gpiochip_close(h)


if __name__ == "__main__":
    main()
