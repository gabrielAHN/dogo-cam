#!/usr/bin/env python3
import json
import logging
import os
import threading
import time

import RPi.GPIO as GPIO

logger = logging.getLogger(__name__)

SERVO1_PIN = 18
SERVO2_PIN = 19
PWM_FREQUENCY = 50
STEP_SIZE = 8
SERVO1_MIN_TILT = 90
MIN_DUTY = 2.0
MAX_DUTY = 12.0
STATE_FILE = "/tmp/servo_positions.json"


class ServoController:
    def __init__(self):
        self.lock = threading.Lock()
        self.servo1_angle = 0
        self.servo2_angle = 0
        self.initialized = False
        self.pwm1 = None
        self.pwm2 = None
        self.last_servo1_time = 0
        self.last_servo2_time = 0
        self.min_movement_interval = 0.02

    def angle_to_duty_cycle(self, angle):
        return MIN_DUTY + (angle / 180.0) * (MAX_DUTY - MIN_DUTY)

    def load_positions(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                self.servo1_angle = state.get("servo1", 90)
                self.servo2_angle = state.get("servo2", 90)
                logger.info(f"Loaded saved positions: Servo1={self.servo1_angle}°, Servo2={self.servo2_angle}°")
                return

            self.servo1_angle = 90
            self.servo2_angle = 90
            logger.info("No saved positions, using defaults: Servo1=90°, Servo2=90°")
        except Exception as e:
            logger.error(f"Error loading servo positions: {e}")
            self.servo1_angle = 90
            self.servo2_angle = 90

    def initialize(self):
        with self.lock:
            if self.initialized:
                return True

            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(SERVO1_PIN, GPIO.OUT)
                GPIO.setup(SERVO2_PIN, GPIO.OUT)

                self.pwm1 = GPIO.PWM(SERVO1_PIN, PWM_FREQUENCY)
                self.pwm2 = GPIO.PWM(SERVO2_PIN, PWM_FREQUENCY)
                self.pwm1.start(0)
                self.pwm2.start(0)

                self.load_positions()
                self.initialized = True
                logger.info("Servos initialized with RPi.GPIO")
                logger.info(f"Servo1 (tilt): {SERVO1_MIN_TILT}°-180°, Servo2 (pan): 0°-180°")
                logger.info(f"Using MG90S calibrated duty cycle: {MIN_DUTY}%-{MAX_DUTY}%")
                return True
            except Exception as e:
                logger.error(f"Error initializing servos: {e}")
                return False

    def _set_servo_position(self, pwm, angle, servo_name):
        duty = self.angle_to_duty_cycle(angle)
        pwm.ChangeDutyCycle(duty)
        logger.info(f"{servo_name} set to {angle}° (duty: {duty:.2f}%)")
        time.sleep(0.03)
        pwm.ChangeDutyCycle(0)
        logger.info(f"{servo_name} PWM stopped - position locked")

    def _move(self, servo, direction):
        if not self.initialized and not self.initialize():
            return False, self.servo1_angle if servo == 1 else self.servo2_angle, True, True

        now = time.time()
        last_move = self.last_servo1_time if servo == 1 else self.last_servo2_time
        if now - last_move < self.min_movement_interval:
            angle = self.servo1_angle if servo == 1 else self.servo2_angle
            return False, angle, True, True

        if servo == 1:
            old_angle = self.servo1_angle
            if direction == "up":
                new_angle = max(SERVO1_MIN_TILT, self.servo1_angle - STEP_SIZE)
            elif direction == "down":
                new_angle = min(180, self.servo1_angle + STEP_SIZE)
            else:
                return False, self.servo1_angle, self.servo1_angle > SERVO1_MIN_TILT, self.servo1_angle < 180

            if new_angle != old_angle:
                logger.info(f"Servo1 {direction}: {old_angle}° → {new_angle}°")
                self._set_servo_position(self.pwm1, new_angle, "Servo1")
                self.servo1_angle = new_angle
                self.last_servo1_time = time.time()
                self.save_positions()

            return True, self.servo1_angle, self.servo1_angle > SERVO1_MIN_TILT, self.servo1_angle < 180

        old_angle = self.servo2_angle
        if direction == "left":
            new_angle = max(0, self.servo2_angle - STEP_SIZE)
        elif direction == "right":
            new_angle = min(180, self.servo2_angle + STEP_SIZE)
        else:
            return False, self.servo2_angle, self.servo2_angle > 0, self.servo2_angle < 180

        if new_angle != old_angle:
            logger.info(f"Servo2 {direction}: {old_angle}° → {new_angle}°")
            self._set_servo_position(self.pwm2, new_angle, "Servo2")
            self.servo2_angle = new_angle
            self.last_servo2_time = time.time()
            self.save_positions()

        return True, self.servo2_angle, self.servo2_angle > 0, self.servo2_angle < 180

    def move_servo1(self, direction):
        with self.lock:
            return self._move(1, direction)

    def move_servo2(self, direction):
        with self.lock:
            return self._move(2, direction)

    def reset_to_home(self):
        with self.lock:
            if not self.initialized and not self.initialize():
                return False

            logger.info(f"Resetting servos: Servo1={self.servo1_angle}° → 90°, Servo2={self.servo2_angle}° → 90°")
            self._set_servo_position(self.pwm1, 90, "Servo1")
            time.sleep(0.2)
            self._set_servo_position(self.pwm2, 90, "Servo2")

            self.servo1_angle = 90
            self.servo2_angle = 90
            self.save_positions()
            logger.info("Reset complete: Servo1=90°, Servo2=90°")
            return True

    def get_position(self):
        with self.lock:
            return {
                "servo1": self.servo1_angle,
                "servo2": self.servo2_angle,
                "can_servo1_up": self.servo1_angle > SERVO1_MIN_TILT,
                "can_servo1_down": self.servo1_angle < 180,
                "can_servo2_left": self.servo2_angle > 0,
                "can_servo2_right": self.servo2_angle < 180,
            }

    def save_positions(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"servo1": self.servo1_angle, "servo2": self.servo2_angle}, f)
        except Exception as e:
            logger.error(f"Error saving servo positions: {e}")

    def cleanup(self):
        with self.lock:
            if not self.initialized:
                return

            try:
                self.save_positions()
                if self.pwm1:
                    self.pwm1.stop()
                if self.pwm2:
                    self.pwm2.stop()
                GPIO.cleanup([SERVO1_PIN, SERVO2_PIN])
                logger.info("Servos cleaned up, positions saved")
            except Exception:
                pass
            self.initialized = False


servo_controller = ServoController()
