FROM ubuntu:22.04

# Install from apt package
RUN apt-get update && apt-get install -y curl python3

# Install arduino-cli
RUN mkdir -p /tool
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/tool sh
ENV PATH $PATH:/tool/
RUN echo $PATH

# Refresh platform indexes
RUN arduino-cli core update-index

# Add ESP32 core
RUN arduino-cli core install esp32:esp32
RUN arduino-cli core list

# Create work directory
RUN mkdir /ESP32C5Controller
WORKDIR /ESP32C5Controller

CMD ["/bin/sh"]
