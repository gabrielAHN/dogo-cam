#!/usr/bin/env python3
"""
MG90S Servo Control using RPi.GPIO - Based on MakersPortal approach
Tower Pro MG90S servos work best with this PWM implementation
"""
import RPi.GPIO as GPIO
import threading
import time
import json
import os
import logging

logger = logging.getLogger(__name__)

# Servo configuration
SERVO1_PIN = 18  # GPIO18 (Pin 12)
SERVO2_PIN = 19  # GPIO19 (Pin 35)
PWM_FREQUENCY = 50  # 50Hz for servo control
STEP_SIZE = 5     # Degrees per step (larger steps for more visible tracking)

# Servo1 (tilt) limits - prevent camera from tilting too far up
SERVO1_MIN_TILT = 90  # Don't tilt higher than 90° (prevents excessive upward tilt during tracking)

# MG90S duty cycle range (from MakersPortal calibration)
# Standard 5-10% causes jitter with MG90S
MIN_DUTY = 2.0   # 0° position
MAX_DUTY = 12.0  # 180° position

# State file for persistence across mode changes
STATE_FILE = '/tmp/servo_positions.json'

class ServoController:
    def __init__(self):
        self.lock = threading.Lock()
        self.servo1_angle = 0  # Start at 0 degrees
        self.servo2_angle = 0  # Start at 0 degrees
        self.initialized = False
        self.pwm1 = None
        self.pwm2 = None

        # Track last movement time for each servo independently
        self.last_servo1_time = 0
        self.last_servo2_time = 0
        self.min_movement_interval = 0.02  # 20ms per servo - responsive manual control (~50 updates/sec max)

    def angle_to_duty_cycle(self, angle):
        """
        Convert angle (0-180) to duty cycle percentage
        MG90S calibrated range: 2-12% (not standard 5-10%)
        Based on MakersPortal testing
        """
        # Linear interpolation from 2% to 12%
        duty = MIN_DUTY + (angle / 180.0) * (MAX_DUTY - MIN_DUTY)
        return duty

    def load_positions(self):
        """Load saved servo positions to maintain position across mode changes"""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.servo1_angle = state.get('servo1', 90)
                    self.servo2_angle = state.get('servo2', 90)
                    logger.info(f"Loaded saved positions: Servo1={self.servo1_angle}°, Servo2={self.servo2_angle}°")
            else:
                # No saved state, use center position
                self.servo1_angle = 90
                self.servo2_angle = 90
                logger.info(f"No saved positions, using defaults: Servo1=90°, Servo2=90°")
        except Exception as e:
            logger.error(f"Error loading servo positions: {e}")
            self.servo1_angle = 90
            self.servo2_angle = 90

    def initialize(self):
        """Initialize GPIO and PWM - servos only move on button clicks"""
        with self.lock:
            if self.initialized:
                return True

            try:
                # Setup GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(SERVO1_PIN, GPIO.OUT)
                GPIO.setup(SERVO2_PIN, GPIO.OUT)

                # Create PWM instances
                self.pwm1 = GPIO.PWM(SERVO1_PIN, PWM_FREQUENCY)
                self.pwm2 = GPIO.PWM(SERVO2_PIN, PWM_FREQUENCY)

                # Start PWM at 0% duty cycle (servos don't move)
                self.pwm1.start(0)
                self.pwm2.start(0)

                # Load saved positions (maintains position across mode changes)
                self.load_positions()

                self.initialized = True
                logger.info(f"Servos initialized with RPi.GPIO")
                logger.info(f"Servo1 (tilt): {SERVO1_MIN_TILT}°-180°, Servo2 (pan): 0°-180°")
                logger.info(f"Using MG90S calibrated duty cycle: {MIN_DUTY}%-{MAX_DUTY}%")
                return True
            except Exception as e:
                logger.error(f"Error initializing servos: {e}")
                return False

    def _set_servo_position(self, pwm, angle, servo_name):
        """
        Set servo position using MakersPortal approach
        Key: Set duty cycle, wait briefly, then set to 0 to reduce jitter
        """
        duty = self.angle_to_duty_cycle(angle)

        # Send PWM signal
        pwm.ChangeDutyCycle(duty)
        logger.info(f"{servo_name} set to {angle}° (duty: {duty:.2f}%)")

        # Wait for servo to reach position (minimal delay for fast tracking)
        time.sleep(0.03)

        # CRITICAL: Set duty to 0 to reduce jitter (MakersPortal technique)
        pwm.ChangeDutyCycle(0)
        logger.info(f"{servo_name} PWM stopped - position locked")

    def move_servo1_smooth(self, direction, steps=2):
        """
        Move Servo 1 multiple steps smoothly (for manual control)
        direction: 'up' or 'down'
        steps: number of steps to move (default 2 for smooth button press feel)
        Returns: (success, new_angle, can_go_up, can_go_down)
        """
        success = False
        for i in range(steps):
            result = self.move_servo1(direction)
            if result[0]:  # If movement was successful
                success = True
                if i < steps - 1:  # Don't sleep on last step
                    time.sleep(0.08)  # Small delay between steps for smoothness
            else:
                break  # Stop if rate limited

        # Return the final state
        return self.move_servo1(direction) if not success else result

    def move_servo1(self, direction):
        """
        Move Servo 1 up or down (single step)
        direction: 'up' or 'down'
        Returns: (success, new_angle, can_go_up, can_go_down)
        """
        with self.lock:
            if not self.initialized and not self.initialize():
                return False, self.servo1_angle, True, True

            # Rate limiting - prevent movements that are too rapid (per servo)
            current_time = time.time()
            time_since_last_move = current_time - self.last_servo1_time
            if time_since_last_move < self.min_movement_interval:
                logger.info(f"Servo1 movement too fast - waiting {self.min_movement_interval - time_since_last_move:.2f}s")
                return False, self.servo1_angle, True, True

            old_angle = self.servo1_angle

            if direction == 'up':
                new_angle = max(SERVO1_MIN_TILT, self.servo1_angle - STEP_SIZE)  # Don't tilt higher than limit
            elif direction == 'down':
                new_angle = min(180, self.servo1_angle + STEP_SIZE)  # Clamp to 180° maximum
            else:
                return False, self.servo1_angle, self.servo1_angle > SERVO1_MIN_TILT, self.servo1_angle < 180

            logger.info(f"Servo1 {direction}: {old_angle}° → {new_angle}°")

            # Only move if angle changed
            if new_angle != old_angle:
                self._set_servo_position(self.pwm1, new_angle, "Servo1")
                self.servo1_angle = new_angle
                self.last_servo1_time = time.time()  # Update servo1 timestamp
                self.save_positions()

            # Check if can continue in each direction
            can_go_up = self.servo1_angle > SERVO1_MIN_TILT
            can_go_down = self.servo1_angle < 180

            return True, self.servo1_angle, can_go_up, can_go_down

    def move_servo2_smooth(self, direction, steps=2):
        """
        Move Servo 2 multiple steps smoothly (for manual control)
        direction: 'left' or 'right'
        steps: number of steps to move (default 2 for smooth button press feel)
        Returns: (success, new_angle, can_go_left, can_go_right)
        """
        success = False
        for i in range(steps):
            result = self.move_servo2(direction)
            if result[0]:  # If movement was successful
                success = True
                if i < steps - 1:  # Don't sleep on last step
                    time.sleep(0.08)  # Small delay between steps for smoothness
            else:
                break  # Stop if rate limited

        # Return the final state
        return self.move_servo2(direction) if not success else result

    def move_servo2(self, direction):
        """
        Move Servo 2 left or right (single step)
        direction: 'left' or 'right'
        Returns: (success, new_angle, can_go_left, can_go_right)
        """
        with self.lock:
            if not self.initialized and not self.initialize():
                return False, self.servo2_angle, True, True

            # Rate limiting - prevent movements that are too rapid (per servo)
            current_time = time.time()
            time_since_last_move = current_time - self.last_servo2_time
            if time_since_last_move < self.min_movement_interval:
                logger.info(f"Servo2 movement too fast - waiting {self.min_movement_interval - time_since_last_move:.2f}s")
                return False, self.servo2_angle, True, True

            old_angle = self.servo2_angle

            if direction == 'left':
                new_angle = max(0, self.servo2_angle - STEP_SIZE)  # Clamp to 0° minimum
            elif direction == 'right':
                new_angle = min(180, self.servo2_angle + STEP_SIZE)  # Clamp to 180° maximum
            else:
                return False, self.servo2_angle, self.servo2_angle > 0, self.servo2_angle < 180

            logger.info(f"Servo2 {direction}: {old_angle}° → {new_angle}°")

            # Only move if angle changed
            if new_angle != old_angle:
                self._set_servo_position(self.pwm2, new_angle, "Servo2")
                self.servo2_angle = new_angle
                self.last_servo2_time = time.time()  # Update servo2 timestamp
                self.save_positions()

            # Check if can continue in each direction
            can_go_left = self.servo2_angle > 0
            can_go_right = self.servo2_angle < 180

            return True, self.servo2_angle, can_go_left, can_go_right

    def set_servo1_angle(self, angle):
        """Set servo1 to exact angle — used by PID controller"""
        with self.lock:
            if not self.initialized and not self.initialize():
                return False
            angle = float(max(SERVO1_MIN_TILT, min(180, angle)))
            self._set_servo_position(self.pwm1, angle, "Servo1")
            self.servo1_angle = angle
            self.last_servo1_time = time.time()
            self.save_positions()
            return True

    def set_servo2_angle(self, angle):
        """Set servo2 to exact angle — used by PID controller"""
        with self.lock:
            if not self.initialized and not self.initialize():
                return False
            angle = float(max(0, min(180, angle)))
            self._set_servo_position(self.pwm2, angle, "Servo2")
            self.servo2_angle = angle
            self.last_servo2_time = time.time()
            self.save_positions()
            return True

    def reset_to_home(self):
        """Reset both servos to center position (90°, 90°)"""
        with self.lock:
            if not self.initialized and not self.initialize():
                return False

            logger.info(f"Resetting servos: Servo1={self.servo1_angle}° → 90°, Servo2={self.servo2_angle}° → 90°")

            # Set both servos to center position
            self._set_servo_position(self.pwm1, 90, "Servo1")
            time.sleep(0.2)  # Small delay between servos
            self._set_servo_position(self.pwm2, 90, "Servo2")

            self.servo1_angle = 90
            self.servo2_angle = 90

            self.save_positions()
            logger.info(f"Reset complete: Servo1=90°, Servo2=90°")
            return True

    def get_position(self):
        """Get current servo positions and available movements"""
        with self.lock:
            return {
                'servo1': self.servo1_angle,
                'servo2': self.servo2_angle,
                'can_servo1_up': self.servo1_angle > SERVO1_MIN_TILT,
                'can_servo1_down': self.servo1_angle < 180,
                'can_servo2_left': self.servo2_angle > 0,
                'can_servo2_right': self.servo2_angle < 180
            }

    def save_positions(self):
        """Save current positions to maintain across mode changes"""
        try:
            state = {
                'servo1': self.servo1_angle,
                'servo2': self.servo2_angle
            }
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"Error saving servo positions: {e}")

    def cleanup(self):
        """Clean up GPIO resources"""
        with self.lock:
            if self.initialized:
                try:
                    # Save positions before cleanup
                    self.save_positions()
                    # Stop PWM
                    if self.pwm1:
                        self.pwm1.stop()
                    if self.pwm2:
                        self.pwm2.stop()
                    # Clean up GPIO
                    GPIO.cleanup([SERVO1_PIN, SERVO2_PIN])
                    logger.info("Servos cleaned up, positions saved")
                except:
                    pass
                self.initialized = False

# Global servo controller instance
servo_controller = ServoController()
