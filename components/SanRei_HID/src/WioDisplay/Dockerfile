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
RUN arduino-cli core update-index --additional-urls https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json

# Add arduino core and libraries
RUN arduino-cli core install arduino:samd
RUN arduino-cli core install Seeeduino:samd --additional-urls https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json

# Install required Arduino libraries
# Note: rpcWiFi / ArduinoOTA (JAndrassy fork, for SAMD51 InternalStorage) are
# provided via host-mounted ../../libraries (see docker-compose.yml).
RUN arduino-cli lib install TFT_eSPI

RUN arduino-cli core list

# Create work directory
RUN mkdir /WioDisplay
WORKDIR /WioDisplay

CMD ["/bin/sh"]
