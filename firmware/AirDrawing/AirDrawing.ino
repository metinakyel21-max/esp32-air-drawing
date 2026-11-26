/*
 * AirDrawing.ino - ESP32 + MPU6050 motion streamer for Air Drawing 3D
 * ---------------------------------------------------------------------------
 * Reads acceleration (m/s^2) and angular rate (rad/s) from an MPU6050 and prints
 * one easy-to-parse line per sample (~100 Hz, 115200 baud):
 *     AX:0.12,AY:0.98,AZ:9.81,GX:0.01,GY:0.02,GZ:0.03
 *
 * Robust by design:
 *   - No sensor?  It never hangs: prints "ERROR:MPU6050_NOT_FOUND" + an I2C scan
 *     once per second and connects by itself as soon as the sensor appears.
 *   - Loose wire? The Adafruit driver keeps returning "true" with all-zero data
 *     when the bus drops. Gravity alone is ~9.8 m/s^2, so an all-zero reading is
 *     treated as a disconnect and the sensor is re-initialised automatically.
 *
 * WIRING (I2C)
 *   MPU6050 VCC -> 3V3  (not 5V)
 *   MPU6050 GND -> GND
 *   MPU6050 SDA -> AIR_SDA_PIN  (default GPIO 8)
 *   MPU6050 SCL -> AIR_SCL_PIN  (default GPIO 9)
 *   AD0 floating or GND -> address 0x68 (0x69 is tried too). XDA/XCL/INT unused.
 *
 *   Different pins? Change the defaults below, or with PlatformIO put
 *   build_flags = -DAIR_SDA_PIN=4 -DAIR_SCL_PIN=5   in a local.ini (see README).
 *   Not sure which pins your wires are on? Flash tools/i2c_pin_finder.
 *
 * ARDUINO IDE
 *   Board package "esp32" (Espressif) · Board "ESP32S3 Dev Module"
 *   Libraries: Adafruit MPU6050, Adafruit Unified Sensor, Adafruit BusIO
 *   USB CDC On Boot: "Enabled" when using the native USB port,
 *                    "Disabled" when using the UART (CH340/CH343/CP210x) port.
 */

#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

#ifndef AIR_SDA_PIN
#define AIR_SDA_PIN 8
#endif
#ifndef AIR_SCL_PIN
#define AIR_SCL_PIN 9
#endif

static const uint32_t I2C_HZ      = 400000;  // fast mode
static const uint32_t SERIAL_BAUD = 115200;
static const uint32_t SAMPLE_MS   = 10;      // ~100 samples/s
static const uint32_t RETRY_MS    = 1000;    // retry interval while the sensor is missing

Adafruit_MPU6050 mpu;
bool     sensorReady = false;
uint32_t lastSample  = 0;
uint32_t lastRetry   = 0;

// Start the sensor at 0x68, falling back to 0x69.
bool initSensor() {
  if (!mpu.begin(0x68, &Wire) && !mpu.begin(0x69, &Wire)) {
    return false;
  }
  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);   // plenty for hand motion, finer resolution
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_44_HZ);     // cuts vibration noise
  return true;
}

// When the sensor is missing, list every device on the bus to help troubleshooting.
void reportMissing() {
  Serial.print("ERROR:MPU6050_NOT_FOUND I2C_DEVICES:");
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) { Serial.printf(" 0x%02X", addr); found++; }
  }
  if (!found) Serial.print(" none");
  Serial.printf(" (SDA=%d:%d SCL=%d:%d)\n", AIR_SDA_PIN, digitalRead(AIR_SDA_PIN),
                AIR_SCL_PIN, digitalRead(AIR_SCL_PIN));
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);                                     // give the serial port a moment
  Wire.begin(AIR_SDA_PIN, AIR_SCL_PIN, I2C_HZ);
  sensorReady = initSensor();
  if (sensorReady) Serial.println("STATUS:MPU6050_READY"); else reportMissing();
}

void loop() {
  uint32_t now = millis();

  if (!sensorReady) {
    if (now - lastRetry >= RETRY_MS) {
      lastRetry = now;
      sensorReady = initSensor();
      if (sensorReady) Serial.println("STATUS:MPU6050_READY"); else reportMissing();
    }
    return;
  }

  if (now - lastSample < SAMPLE_MS) return;
  lastSample = now;

  sensors_event_t a, g, temp;
  if (!mpu.getEvent(&a, &g, &temp) ||
      fabsf(a.acceleration.x) + fabsf(a.acceleration.y) + fabsf(a.acceleration.z) < 0.5f) {
    sensorReady = false;                          // bus dropped: re-init on the next retry
    Serial.println("ERROR:MPU6050_READ_FAILED");
    return;
  }

  char line[96];
  snprintf(line, sizeof(line), "AX:%.2f,AY:%.2f,AZ:%.2f,GX:%.2f,GY:%.2f,GZ:%.2f",
           a.acceleration.x, a.acceleration.y, a.acceleration.z,
           g.gyro.x, g.gyro.y, g.gyro.z);
  Serial.println(line);
}
