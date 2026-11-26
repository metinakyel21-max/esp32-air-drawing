// I2C pin finder: which GPIOs are my SDA/SCL wires really on?
// Tries every pin pair x every I2C address and prints the hits, plus which pins read HIGH
// with an internal pull-down (= connected to the sensor's pull-up resistors).
//
// How to read the result: the sensor's pull-ups make both wires read "=1". If you see
// "FOUND SDA=X SCL=Y" for many different SCL values, the real SCL is the FIRST one listed
// (Wire.end() does not always release the previous SCL pin, so later pairs are false hits).
#include <Arduino.h>
#include <Wire.h>

// ESP32-S3 GPIOs that are usually broken out (USB 19/20 and flash/PSRAM pins excluded where sensible).
const int P[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 21,
                 38, 39, 40, 41, 42, 45, 46, 47, 48};
const int N = sizeof(P) / sizeof(P[0]);

void setup() { Serial.begin(115200); delay(500); }

void loop() {
  Serial.print("PIN_LEVEL(with pulldown):");
  for (int i = 0; i < N; i++) {
    pinMode(P[i], INPUT_PULLDOWN);
    delay(2);
    if (digitalRead(P[i])) Serial.printf(" %d=1", P[i]);
  }
  Serial.println();
  int hits = 0;
  for (int i = 0; i < N; i++)
    for (int j = 0; j < N; j++) {
      if (i == j) continue;
      Wire.end();
      if (!Wire.begin(P[i], P[j], 100000)) continue;
      Wire.setTimeOut(3);
      for (uint8_t a = 0x08; a < 0x78; a++) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) {
          Serial.printf("FOUND SDA=%d SCL=%d addr=0x%02X\n", P[i], P[j], a);
          hits++;
        }
      }
    }
  Serial.printf("SCAN_DONE hits=%d\n", hits);
  delay(1000);
}
