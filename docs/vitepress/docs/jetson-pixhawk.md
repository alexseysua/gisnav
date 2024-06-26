# Pixhawk & Jetson Nano HIL

::: tip NVIDIA Jetson Nano discontinued
This article was created for an older version of GISNav and since its publication, NVIDIA Jetson Nano has been discontinued. This article is provided for reference until an updated article on HIL simulation is written.

:::

::: info Todo
GISNav `v0.65.0` now comes with `arm64` base images. Update section on QEMU.

:::

This section provides an example on how to run GISNav on a Jetson Nano in a PX4 HIL simulation on a Pixhawk FMU. This example uses the [NXP FMUK66-E board](https://docs.px4.io/main/en/flight_controller/nxp_rddrone_fmuk66.html) as an example but any [PX4 supported board](https://px4.io/autopilots/) should work.

::: warning Warning: Propellers off
Keep the propellers **off** your drone throughout the HIL simulation.

:::

### Prerequisites

[Install GISNav locally](./install-locally)

### Connect Jetson Nano and Pixhawk

In this example, we will power the Pixhawk from the development computer via USB and the Jetson Nano from a wall socket via a DC adapter to avoid having to handle LiPo batteries. In a more realistic setup, you would supply power to both boards from the onboard battery.

Follow the below steps and diagram to setup your HIL simulation hardware:

- **Install a bootloader on your Pixhawk board if your board does not yet have one.** See your board manufacturer's instructions on how to load one onto your specific board.
- Connect your Jetson Nano to your development computer via Ethernet cable (see Jetson Nano SITL for more information).
- Connect your Jetson Nano to a wall socket using a micro-USB power adapter.
- Connect your Pixhawk board to your development computer via micro-USB cable.
- Connect your Pixhawk board to your Jetson Nano via TELEM1 (set PX4 `XRCE_DDS_0_CFG` parameter value to `101`) using a USB to UART converter.

#### Diagram

```mermaid
graph TB
    subgraph "FMUK66-E (FMU)"
        subgraph "TELEM1"
            FMU_TELEM1_RX[RX]
            FMU_TELEM1_TX[TX]
            FMU_TELEM1_GND[GND]
        end
        FMU_USB[micro-USB Port]
    end
    subgraph "Development computer"
        Laptop_ETH[Ethernet Port]
        Laptop_USB[USB Port]
    end
    subgraph "Jetson Nano"
        Nano_USB[USB Port x4]
        Nano_micro_USB[Micro USB Port]
        Nano_HDMI[HDMI Port]
        Nano_ETH[Ethernet]
    end
    subgraph "USB to UART Converter"
        Converter_RX[RX]
        Converter_TX[TX]
        Converter_GND[GND]
        Converter_USB[USB]
    end
    Socket[Wall Socket]
    subgraph "Optional (can also use RDP or VNC)"
        Display[External Display]
        Mouse[USB Mouse]
        Keyboard[USB Keyboard]
    end
    FMU_TELEM1_TX ---|To UART RX| Converter_RX
    FMU_TELEM1_RX ---|To UART TX| Converter_TX
    FMU_TELEM1_GND ---|To UART GND| Converter_GND
    FMU_USB ---|To Dev Computer USB| Laptop_USB
    Converter_USB ---|To Nano USB| Nano_USB
    Nano_micro_USB ---|Micro USB Power| Socket
    Nano_HDMI ---|HDMI| Display
    Nano_USB ---|USB| Mouse
    Nano_USB ---|USB| Keyboard
    Nano_ETH ---|To Dev Computer ETH| Laptop_ETH
```

#### Picture

![NXP FMUK66-E Setup](/public/gisnav_hil_fmuk66-e_setup.jpg)

* NXP FMUK66-E (FMU) board connected to laptop via micro-USB and to Jetson Nano via TELEM1.
* FMU draws power from laptop via micro-USB, and Jetson Nano from wall socket via dedicated micro-USB DC adapter, so no LiPo batteries needed.
* Connection from FMU to Jetson Nano via TELEM1 serial port using USB to UART converter. See [FMUK66-E revision C pin layout](https://nxp.gitbook.io/hovergames/rddrone-fmuk66/connectors/telemetry-1) for how to wire the TELEM1 JST-GH connector (only GND, RX and TX used here).
* Other wires as per [manufacturer's instructions](https://nxp.gitbook.io/hovergames/userguide/assembly/connecting-all-fmu-wires), except for missing telemetry radio.

::: info TX to RX to TX
The TX from one board connects to the RX of the other board and vice versa.

:::

### Install QEMU emulators

Install QEMU emulators on your Jetson Nano to make `linux/amd64` images run on the `linux/arm64` Jetson Nano:

```bash
docker run --privileged --rm tonistiigi/binfmt --install all
```

[QEMU Documentation](https://docs.docker.com/build/building/multi-platform/#building-multi-platform-images)

### Upload PX4 firmware

See the [PX4 uploading firmware instructions](https://docs.px4.io/main/en/dev_setup/building_px4.html#uploading-firmware-flashing-the-board) for how to upload your development version of PX4 onto your Pixhawk board. To find the `make` target for your specific board, list all options with the `make list_config_targets` command:

```bash
cd ~/colcon_ws/src/gisnav/docker
export COMPOSE_PROJECT_NAME=gisnav
docker compose run px4 make list_config_targets
```

Then choose your appropriate board for the following examples. We are going to choose `nxp_fmuk66-e_default` for this example:

```bash
export COMPOSE_PROJECT_NAME=gisnav
docker compose run px4 make distclean
docker compose run px4 make nxp_fmuk66-e_default upload
```

### Deploy offboard services

The following steps to deploy the offboard services are based on the [PX4 HIL simulation instructions](https://docs.px4.io/main/en/simulation/hitl.html). The `px4` Docker compose service has a custom `iris_hitl` model and a `hitl_iris_ksql_airport.world` Gazebo world that we are going to use in this example:

```bash
export COMPOSE_PROJECT_NAME=gisnav
docker compose run -e DONT_RUN=1 px4 make px4_sitl_default gazebo-classic
docker compose run px4 source Tools/simulation/gazebo-classic/setup_gazebo.bash $(pwd) $(pwd)/build/px4_sitl_default
docker compose run px4 gazebo Tools/simulation/gazebo-classic/sitl_gazebo-classic/worlds/hitl_iris_ksql_airport.world

# Important: Start QGroundControl last
docker compose up qgc
```

After deploying the HIL simulation, adjust the settings via the QGC application as follows:

- Precisely match the `COM_RC_IN_MODE` parameter setting if mentioned in the instructions.
- Ensure that you have HITL enabled in QGC Safety settings.
- Ensure you have the virtual joystick enabled in QGC General settings.

### Deploy onboard services

Once you have the HIL simulation running, login to your Jetson Nano and deploy the onboard services:

```bash
mkdir -p ~/colcon_ws/src
cd ~/colcon_ws/src
git clone https://github.com/hmakelin/gisnav.git
cd ~/colcon_ws/src/gisnav
make -C docker up-onboard-hil-px4
```
