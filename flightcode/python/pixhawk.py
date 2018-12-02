#!/usr/bin/env python

# Pixhawk Initialization and Firmware Upgrade

import ConfigParser
import glob
import json
import logging
import logging.config
import os
import re
import serial
import subprocess
import sys
import time
import traceback
from pymavlink import mavutil
sys.path.append("/usr/bin")
import clock
import led
import usb

version_file = "/PIX_VERSION"
sololink_conf = "/etc/sololink.conf"
sololink_back = "/etc/sololink.back"
config_baud_name = "telemBaud"
config_baudlist_name = "telemBaudList"
config_dev_name = "telemDev"
config_flow_name = "telemFlow"
firmware_root = "/firmware"
firmware_root_3dr = "/firmware/3dr"
firmware_root_green = "/firmware/green"
firmware_root_wipe = "/firmware/wipe"
cube_version = "unknown"


# ardupilot/libraries/AP_SerialManager/AP_SerialManager.cpp, supported bauds:
#   1500000 921600 500000 460800 230400 115200 111100 100000 57600 38400 19200
#   9600 4800 2400 1200
# Reordered in an attempt to try more likely ones sooner (guesses). The one in
# the config file is always tried first.
baudlist_default = "\
115200 \
921600 \
57600 \
230400 \
460800 \
1500000 \
"


# return a matching file, or None
def glob_file(pattern):
    files = glob.glob(pattern)
    if len(files) == 0:
        return None
    elif len(files) > 1:
        logger.warning("more than one matching filename (%s)", str(files))
    return files[0]


# /dev/serial/by-id/usb-3D_Robotics_PX4_FMU_v2.x_0-if00
# /dev/serial/by-id/usb-3D_Robotics_PX4_BL_FMU_v2.x_0-if00
dev_pattern_usb = "/dev/serial/by-id/usb*"

# return device name or None
def create_usb_serial(timeout=5):
    usb.init()
    usb.enable()
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    now_us = start_us
    timeout_us = timeout * 1000000
    while glob_file(dev_pattern_usb) is None and (now_us - start_us) < timeout_us:
        now_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        time.sleep(0.001)
    end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    dev_name = glob_file(dev_pattern_usb)
    if os.path.exists(dev_name):
        logger.info("%s created in %0.3f sec",
                     dev_name, (end_us - start_us) / 1000000.0)
    else:
        logger.info("creating %s", dev_pattern_usb)
        usb.disable()
        usb.uninit()
    return dev_name


# return True (success) or False (did not delete device)
def delete_usb_serial(timeout=1):
    usb.disable()
    usb.uninit()
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    now_us = start_us
    timeout_us = timeout * 1000000
    while glob_file(dev_pattern_usb) is not None and (now_us - start_us) < timeout_us:
        now_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        time.sleep(0.001)
    end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    if glob_file(dev_pattern_usb) is None:
        logger.debug("%s deleted in %0.3f sec",
                     dev_pattern_usb, (end_us - start_us) / 1000000.0)
        return True
    else:
        logger.error("deleting device %s", dev_pattern_usb)
        return False


def config_get(cfg, name):
    try:
        value = cfg.get("solo", name)
    except:
        value = None
    return value


def config_getint(cfg, name):
    try:
        value = cfg.getint("solo", name)
    except:
        value = None
    return value


def config_getbool(cfg, name, default=None):
    try:
        value = cfg.getboolean("solo", name)
    except:
        value = default
    return value


def config_write(config):
    os.system("cp %s %s" % (sololink_conf, sololink_back))
    os.system("md5sum %s > %s.md5" % (sololink_back, sololink_back))
    os.system("sync")
    f = open(sololink_conf, 'wb')
    config.write(f)
    f.close()
    os.system("md5sum %s > %s.md5" % (sololink_conf, sololink_conf))
    os.system("sync")
    os.system("rm %s %s.md5" % (sololink_back, sololink_back))


def create_tty_mavlink(serial_baud=None):
    config = ConfigParser.SafeConfigParser()
    config.optionxform = str
    config.read(sololink_conf)

    serial_dev = config_get(config, config_dev_name)
    if serial_dev is None:
        logger.error("reading %s from %s", config_dev_name, sololink_conf)
        return None
    logger.debug("%s = %s", config_dev_name, serial_dev)

    serial_flow = config_getbool(config, config_flow_name, True)
    logger.debug("%s = %s", config_flow_name, serial_flow)

    if serial_baud is None:
        serial_baud = config_getint(config, config_baud_name)
        if serial_baud is None:
            logger.error("reading %s from %s", config_baud_name, sololink_conf)
            return None
        logger.debug("%s = %d", config_baud_name, serial_baud)

    m = mavutil.mavlink_connection(serial_dev, baud=serial_baud)
    m.set_rtscts(serial_flow)
    return m


# Get list of baud rates to try
#
# Returns:
#     list of integers
def get_baudlist(config=None, expected_baud=None):

    # use list in config file if it is there
    baudlist = config_get(config, config_baudlist_name)
    if baudlist is None:
        # not in config file; use default
        baudlist = baudlist_default
        logger.debug("using default baud list")

    # convert from string to list of integers
    baudlist = baudlist.split()
    baudlist = [int(b) for b in baudlist]

    # put expected baud at start, twice (one retry)
    if expected_baud is not None:
        # first delete it from wherever it might be
        try:
            baudlist.remove(expected_baud)
        except:
            # config file baud is not one we were going to try!
            logger.warning("trying unlisted baud %d", expected_baud)
        # insert at beginning
        baudlist.insert(0, expected_baud)
        baudlist.insert(0, expected_baud)

    return baudlist


# Find and return Pixhawk's telemetry baud
#
# The assumption is that the baud in the config file is almost always correct.
# If the baud is changed via a GCS, this function scans a list of bauds,
# looking for a heartbeat on each one. When a heartbeat is found, the config
# file is updated and that baud is returned.
#
# Config items:
#   serial device (required)
#   expected baud (optional, default is to scan list)
#   list of bauds to scan (optional, default is 'baudlist_default')
#
# The expected baud is given preference by putting it first in the list of
# bauds to scan. It is actually put at the head of the list twice to give some
# degree of error tolerance.
#
# Returns:
#     baud if found
#     None if Pixhawk not detected or other error
def get_baud():

    config = ConfigParser.SafeConfigParser()
    config.optionxform = str
    config.read(sololink_conf)

    serial_dev = config_get(config, config_dev_name)
    if serial_dev is None:
        logger.error("reading %s from %s", config_dev_name, sololink_conf)
        return None
    logger.debug("%s = %s", config_dev_name, serial_dev)

    serial_flow = config_getbool(config, config_flow_name, True)
    logger.debug("%s = %s", config_flow_name, serial_flow)

    expected_baud = config_getint(config, config_baud_name)
    if expected_baud is None:
        logger.warning("no %s in %s (will search)", config_baud_name, sololink_conf)
    logger.debug("%s = %s", config_baud_name, str(expected_baud))

    baudlist = get_baudlist(config, expected_baud)

    logger.debug("baud list %s", str(baudlist))

    logger.info("checking baud...")

    for baud in baudlist:

        logger.debug("baud %d...", baud)

        m = mavutil.mavlink_connection(serial_dev, baud=baud)
        m.set_rtscts(serial_flow)
        # Allow for missing one, then getting the next one. The expectation is
        # that we'll almost always have the right baud and get the first
        # heartbeat.
        start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        hb = m.recv_match(type='HEARTBEAT', blocking=True, timeout=2.5)
        end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        m.close()

        if hb is not None:
            # pixhawk is at baud
            # update config file if necessary
            logger.debug("heartbeat received in %0.3f sec",
                         (end_us - start_us) / 1000000.0)
            logger.info("found at %d", baud)
            if baud != expected_baud:
                logger.info("updating %s %s from %s to %d", sololink_conf,
                            config_baud_name, str(expected_baud), baud)
                config.set("solo", config_baud_name, str(baud))
                config_write(config)
            return baud
        logger.info("not at %d", baud)
    logger.error("not detected at any baud")
    return None


# Set baud rate in config file if necessary
def set_baud(new_baud):

    config = ConfigParser.SafeConfigParser()
    config.optionxform = str
    config.read(sololink_conf)

    old_baud = config_getint(config, config_baud_name)
    if old_baud is None:
        logger.warning("no %s in %s", config_baud_name, sololink_conf)
        return
    logger.debug("%s = %s", config_baud_name, str(old_baud))

    if new_baud != old_baud:
        logger.info("updating %s %s from %s to %d", sololink_conf,
                    config_baud_name, str(old_baud), new_baud)
        config.set("solo", config_baud_name, str(new_baud))
        config_write(config)


# Get version of running firmware
#
# The autopilot_version_request message is used to get the git hashes. A
# param_request_list message is used to get the build number (x.y.z); it
# happens to be returned in one of the statustext message sent as part of the
# reply.
#
# Returned version is a dictionary:
#   keys are: build_version, ardupilot_git_hash, px4_git_hash, nuttx_git_hash
#   each value is a string, or missing if could not get version from pixhawk
def get_version():

    config = ConfigParser.SafeConfigParser()
    config.optionxform = str
    config.read(sololink_conf)

    serial_dev = config_get(config, config_dev_name)
    if serial_dev is None:
        logger.error("reading %s from %s", config_dev_name, sololink_conf)
        return None
    logger.debug("%s = %s", config_dev_name, serial_dev)

    serial_flow = config_getbool(config, config_flow_name, True)
    logger.debug("%s = %s", config_flow_name, serial_flow)

    serial_baud = config_getint(config, config_baud_name)
    if serial_baud is None:
        logger.error("reading %s from %s", config_baud_name, sololink_conf)
        return None
    logger.debug("%s = %d", config_baud_name, serial_baud)

    m = mavutil.mavlink_connection(serial_dev, baud=serial_baud)
    m.set_rtscts(serial_flow)

    version = {}

    # Read version. Use AUTOPILOT_VERSION message to get the hashes, and use
    # the STATUSTEXT returned at the start a parameter dump to get the x.y.z
    # version.

    m.mav.autopilot_version_request_send(m.target_system, m.target_component)
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    av = m.recv_match(type='AUTOPILOT_VERSION', blocking=True, timeout=2)
    end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    if av is not None:
        version["ardupilot_git_hash"] = "".join(chr(e) for e in av.flight_custom_version)
        version["px4_git_hash"] =  "".join(chr(e) for e in av.middleware_custom_version)
        version["nuttx_git_hash"] = "".join(chr(e) for e in av.os_custom_version)
        logger.debug("git hashes received in %0.3f sec",
                     (end_us - start_us) / 1000000.0)
    else:
        logger.warning("no version hashes received")

    m.mav.param_request_list_send(m.target_system, m.target_component)
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    end_us = start_us
    timeout_us = 5 * 1000000
    # Loop because we might get a STATUSTEXT without the version first.
    while (end_us - start_us) < timeout_us:
        st = m.recv_match(type='STATUSTEXT', blocking=True, timeout=5)
        end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        if st is not None:
            logger.info("Status Text: %s" %st)
            # "APM:Copter solo-0.1.2 (b2dacc52)"
            match = re.match("APM:.*?solo-([0-9]+\.[0-9]+\.[0-9]+)", st.text)
            if match:
                logger.debug("build version received in %0.3f sec",
                             (end_us - start_us) / 1000000.0)
                version["build_version"] = match.group(1)
                logger.info("build version %s", version_string(version))
                m.close()
                return version
            # "ArduCopter V3.2.1 (b2dacc52)"
            # This is matched in case someone is messing with their firmware
            # Anything looking like a version x.y.z is pulled out
            match = re.match(".*?([0-9]+\.[0-9]+\.[0-9]+)", st.text)
            if match:
                logger.warning("firmware is not specifically for solo")
                logger.info("build version received in %0.3f sec",
                             (end_us - start_us) / 1000000.0)
                version["build_version"] = match.group(1)
                logger.info("build version %s", version_string(version))
                m.close()
                return version
    ### while end_us...

    m.close()
    logger.warning("no build version received")
    return version

# Checking param INS_ACC_ID to see if this is stock Pixhawk 2.0 or a Pixhawk 2.1
#  - A pixhawk 2.1 will have INS_ACC_ID, and it will be 1442082 (return true)
#  - A pixhawk 2.0 running master will have INS_ACC_ID, but will not be 1442082 (return false)
#  - A pixhawk 2.0 with stock 3DR firmware will not have INS_ACC_ID at all (return false)
def checkPixhawkVersion():
    global cube_version
    # Setup serial comm to the pixhawk
    config = ConfigParser.SafeConfigParser()
    config.optionxform = str
    config.read(sololink_conf)

    serial_dev = config_get(config, config_dev_name)
    if serial_dev is None:
        logger.error("reading %s from %s", config_dev_name, sololink_conf)
        return False
    logger.debug("%s = %s", config_dev_name, serial_dev)

    serial_flow = config_getbool(config, config_flow_name, True)
    logger.debug("%s = %s", config_flow_name, serial_flow)

    serial_baud = config_getint(config, config_baud_name)
    if serial_baud is None:
        logger.error("reading %s from %s", config_baud_name, sololink_conf)
        return False
    logger.debug("%s = %d", config_baud_name, serial_baud)
    m = mavutil.mavlink_connection(serial_dev, baud=serial_baud)
    m.set_rtscts(serial_flow)
    timeout_us = 30 * 1000000

    # First we'll check parameter INS_ACC_ID. On stock 3DR firmware, this param
    # will not be found.  With ArduCopter 3.5+, the parameter will be detected.
    # A value of 1442082 is a green cube.  A value of 1245474 is a stock cube.
    logger.info("Checking parameter INS_ACC_ID to determine cube version")
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    end_us = start_us
    while ((end_us - start_us) < timeout_us) and cube_version == 'unknown':
        m.mav.param_request_read_send(m.target_system, m.target_component, 'INS_ACC_ID', -1)
        param_check = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=5)
        end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        if param_check is not None:
            if param_check.get_type() == 'PARAM_VALUE' and str(param_check.param_id) == 'INS_ACC_ID':
                if str(param_check.param_value) ==  '1442082.0':
                    logger.info("Pixhawk 2.1 on board!")
                    cube_version = 'cube_21'
                    m.close()
                    return True
                elif str(param_check.param_value) ==  '1245474.0': 
                    logger.info("Pixhawk 2.0 on board. But not stock solo firmware")
                    cube_version = 'cube_20'
                    m.close()
                    return True
        logger.info("cube_version = %s", cube_version)
    
    # If we've gotten to this point, it must have not found the INS_ACC_ID. So
    # we'll check the compass #3 device ID instead. On a stock cube with stock
    # stock FW, it will be 66286.  On a stock with AC3.5, it is for some reason
    # not found. So this only works for stock FW on a stock cube, which should
    # be fine for all but a few people running master on a stock cube.
    logger.info("Checking parameter COMPASS_DEV_ID3 to determine cube version")
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    end_us = start_us
    while ((end_us - start_us) < timeout_us) and cube_version == 'unknown':
        m.mav.param_request_read_send(m.target_system, m.target_component, 'COMPASS_DEV_ID3', -1)
        param_check = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=5)
        end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        if param_check is not None:
            if param_check.get_type() == 'PARAM_VALUE' and str(param_check.param_id) == 'COMPASS_DEV_ID3':
                if str(param_check.param_value) ==  '263178.0':
                    logger.info("Pixhawk 2.1 on board!")
                    cube_version = 'cube_21'
                    m.close()
                    return True
                elif str(param_check.param_value) ==  '66826.0':
                    logger.info("Pixhawk 2.0 on board. You should go green :)")
                    cube_version = 'cube_20'
                    m.close()
                    return True
        logger.info("cube_version = %s", cube_version)
    
    # Handle scenario where the external compass is unplugged, so what was
    # compass #3 has become #2.
    logger.info("Checking parameter COMPASS_DEV_ID2 to determine cube version")
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    end_us = start_us
    while ((end_us - start_us) < timeout_us) and cube_version == 'unknown':
        m.mav.param_request_read_send(m.target_system, m.target_component, 'COMPASS_DEV_ID2', -1)
        param_check = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=5)
        end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        if param_check is not None:
            if param_check.get_type() == 'PARAM_VALUE' and str(param_check.param_id) == 'COMPASS_DEV_ID2':
                if str(param_check.param_value) ==  '263178.0':
                    logger.info("Pixhawk 2.1 on board!")
                    cube_version = 'cube_21'
                    m.close()
                    return True
                elif str(param_check.param_value) ==  '66826.0':
                    logger.info("Pixhawk 2.0 on board. You should go green :)")
                    cube_version = 'cube_20'
                    m.close()
                    return True
        logger.info("cube_version = %s", cube_version)
    
    # If we've reached this point, we were unable to positively identify which
    # cube is in the solo. Probably because something is malfunctioning, or
    # because someone is running master on a stock cube. In either case, there
    # is nothing more we can do, so returning false and leave cube_version unknown.
    cube_version = 'unknown'
    logger.info("Unable to determine cube version")
    return False

def removeFWfiles(dir):
    if dir == '3dr':
        filelist=glob.glob('/firmware/3dr/*.*')
    elif dir == 'green':
        filelist=glob.glob('/firmware/green/*.*')
    elif dir == 'main':
        filelist=glob.glob('/firmware/*.px4')
    for file in filelist:
        os.remove(file)

# load firmware
def load(firmware_path):
    logger.info("loading %s", firmware_path)

    dev_name = create_usb_serial()
    if dev_name is None:
        logger.info("Failed to open USB serial port to load firmware")
        return False

    # load firmware
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    uploader = subprocess.Popen(["px_uploader.py",
                           "--port=%s" % dev_pattern_usb,
                           firmware_path])

    ## Start a 2 minute timeout for loading firmware in case it fails
    delay = 1.0
    timeout = int(120 / delay)                      
    while uploader.poll() is None and timeout > 0:
        time.sleep(delay)
        timeout -= delay                       

    if uploader.poll() is None:
        #px_uploader.py failed to complete, probably hosed
        loaded = False
        logger.info("Firmware loading failed due to timeout.")
    elif uploader.returncode != 0:
        #px_uploader.py returned error, failed to load due to error
        loaded = False
        logger.info("Firmware loading failed due to error.")
    elif uploader.returncode == 0:
        #px_uploader succeeded
        loaded = True
        end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        logger.info("firmware loaded in %0.3f sec",
                    (end_us - start_us) / 1000000.0)
    else:
        #Unsure if loading FW worked or not, so we'll proceed
        loaded = True
        end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        logger.info("firmware may have loaded. Checking for HB")

    if loaded:
        # firmware loaded; wait for heartbeat on telemetry port
        # This allows for the baud rate to change along with the firmware load.
        start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
        # 0.6.x ... 1.0.5 has a pixhawk build at 115200 baud
        # internal versions might have a pixhawk build at 921600  baud
        # 1.1.5 and later have a pixhawk build at 115200 baud
        baudlist = get_baudlist(None, 115200)
        hb_quit = False
        hb_time_us = None
        wait_us = None
        hb = None
        while not hb_quit:
            for baud in baudlist:
                logger.debug("trying %d", baud)
                m = create_tty_mavlink(baud)
                if m is None:
                    logger.error("creating tty mavlink connection")
                    hb_quit = True
                    break # for baud
                # flush input - could we have data from before flashing?
                flush_bytes = m.port.inWaiting()
                if flush_bytes > 0:
                    logger.info("flushing %d bytes", flush_bytes)
                    m.port.flushInput()
                logger.debug("waiting for heartbeat")
                hb = m.recv_match(type='HEARTBEAT', blocking=True, timeout=1.1)
                wait_us = clock.gettime_us(clock.CLOCK_MONOTONIC) - start_us
                if hb is None:
                    logger.debug("timeout waiting for first heartbeat")
                else:
                    # insisting on a second heartbeat was in response to
                    # occasionally detecting a heartbeat at the wrong baud
                    # when the baud changes with the firmware (!)
                    logger.debug("got first heartbeat")
                    hb_time_us = clock.gettime_us(clock.CLOCK_MONOTONIC) - start_us
                    hb = m.recv_match(type='HEARTBEAT', blocking=True, timeout=1.1)
                    if hb is None:
                        # this has been observed to happen (rarely)
                        logger.info("timeout waiting for second heartbeat at %d after loading", baud)
                        # ...and continue with the next baud rate
                    else:
                        logger.debug("got second heartbeat")
                m.close()
                if hb is not None:
                    set_baud(baud)
                    hb_quit = True
                    break # for baud
                # 0.1.0 comes up in about 25 sec
                # 1.0.8 comes up in about 8 sec
                # 1.1.4 comes up in about 15 sec
                if wait_us > 45000000: # 45 sec
                    hb_quit = True # to exit the while loop
                    break # exit the baud loop
            ### for baud in baudlist
        ### while !hb_quit
        if hb is None:
            logger.error("timeout waiting for heartbeat after loading")
            loaded = False
        else:
            logger.info("heartbeat after loading in %0.3f sec", hb_time_us / 1000000.0)

    delete_usb_serial()

    return loaded


# check usb (diagnostic)
def check_usb():
    dev_name = create_usb_serial()
    if dev_name is None:
        print "ERROR creating usb serial device"
        return
    print "created %s" % dev_name
    time.sleep(1)
    s = serial.Serial(port=dev_name, timeout=1)
    print "opened %s" % dev_name
    d = s.read(100)
    print [hex(ord(b)) for b in d]
    s.close()
    print "closed %s" % dev_name
    time.sleep(1)
    delete_usb_serial()
    print "deleted %s" % dev_name


# verify usb (test)
# return True (pass) or False (fail)
def verify_usb():
    dev_name = create_usb_serial()
    if dev_name is None:
        logger.info("ERROR creating usb serial device")
        return False
    # need to experimentally determine how long we have to wait here before
    # trying to use the USB device (pretty sure it's nonzero)
    time.sleep(1)
    m = mavutil.mavlink_connection(dev_name)
    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    hb = m.recv_match(type='HEARTBEAT', blocking=True, timeout=2.5)
    end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    m.close()
    delete_usb_serial()
    if hb is not None:
        logger.info("heartbeat received over USB in %0.3f sec",
                    (end_us - start_us) / 1000000.0)
        return True
    else:
        logger.error("ERROR getting heartbeat over USB")
        return False


# return info about firmware file
# (full_path, versions)
# full_path is the full path to the file, or None if no file
# if full_path is not None, versions is a dictionary of version info
def find_firmware(dir):
    files = glob.glob("%s/*.px4" % dir)
    if len(files) == 0:
        return (None, None)
    # read git hashes
    full_path = files[-1]
    f = open(full_path)
    info = json.loads(f.read())
    f.close()
    versions = {}
    for i in [ "ardupilot_git_hash", "px4_git_hash", "nuttx_git_hash" ]:
        try:
            versions[i] = str(info[i][:8])
        except:
            versions[i] = "(missing)"
    return (full_path, versions)


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError:
        pass # already exists


def move_firmware(firmware_path):
    mkdir_p("%s/loaded" % firmware_root)
    firmware_file = os.path.basename(firmware_path)
    os.rename(firmware_path, "%s/loaded/%s" % (firmware_root, firmware_file))


def write_version_file(version):
    f = open(version_file, "w")
    for v in ["build_version", "ardupilot_git_hash", "px4_git_hash", "nuttx_git_hash"]:
        if v in version:
            f.write(version[v] + '\n')
        else:
            f.write("unknown\n")
    f.close()


def version_string(version):
    vs = ""
    if "build_version" in version:
        vs += "build=%s " % version["build_version"]
    vs += "ardupilot="
    if "ardupilot_git_hash" in version:
        vs += version["ardupilot_git_hash"]
    else:
        vs += "unknown"
    vs += " px4="
    if "px4_git_hash" in version:
        vs += version["px4_git_hash"]
    else:
        vs += "unknown"
    vs += " nuttx="
    if "nuttx_git_hash" in version:
        vs += version["nuttx_git_hash"]
    else:
        vs += "unknown"
    return vs

def resetParameters():
    logger.info("   Resetting parameters...")
    config = ConfigParser.SafeConfigParser()
    config.optionxform = str
    config.read(sololink_conf)

    serial_dev = config_get(config, config_dev_name)
    if serial_dev is None:
        return

    serial_flow = config_getbool(config, config_flow_name, True)

    serial_baud = config_getint(config, config_baud_name)
    if serial_baud is None:
        return

    m = mavutil.mavlink_connection(serial_dev, baud=serial_baud)
    m.set_rtscts(serial_flow)
    
    m.mav.param_set_send(m.target_system, m.target_component, 'SYSID_SW_MREV', 0, 0)
    time.sleep(5)
    m.close()
    return

def recoveryCheck():
    p = subprocess.Popen(['df', '-h'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p1, err = p.communicate()
    pattern = p1

    if pattern.find("mmcblk0p1") == -1:
        logger.info("Not on recovery partition")
        return False
    else:
        logger.info("On recovery partition")
        return True

def rebootPixhawk():
    logger.info("   Rebooting pixhawk...")
    global cube_version
    config = ConfigParser.SafeConfigParser()
    config.optionxform = str
    config.read(sololink_conf)

    serial_dev = config_get(config, config_dev_name)
    if serial_dev is None:
        return

    serial_flow = config_getbool(config, config_flow_name, True)

    serial_baud = config_getint(config, config_baud_name)
    if serial_baud is None:
        return

    m = mavutil.mavlink_connection(serial_dev, baud=serial_baud)
    m.set_rtscts(serial_flow)

    m.mav.command_long_send(m.target_system, m.target_component,246 , 0, 1, 0, 0, 0, 0, 0, 0)
    m.close()
    logger.info("   Waiting 60 seconds for pixhawk to reboot...")
    time.sleep(60)
    return

def initialize():
    global cube_version
    firmware_path = None
    paramsAfterLoad = False

    start_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    led.blink(1000, 100)
    print "pixhawk..."
    baud = get_baud()
    if baud == None:
        print "pixhawk: ERROR checking baud"
        logger.error("finding baud")
        logger.error("pixhawk status: no response")
        # pixhawk might be stuck in bootloader

    # If we're in a factory reset, we just need to reset the pixhawk.
    # If we've just completed the clean install, load the appropriate ArduCopter FW and reset the pixhawk
    # Otherwise, just look in /firmware and load whatever is there.
    if recoveryCheck():
        resetParameters()
        rebootPixhawk()
    else:
        if (os.path.isdir("/firmware/3dr") or os.path.isdir("/firmware/green")) and checkPixhawkVersion():
            # This must be a clean install if the initial FW directories are here.
            paramsAfterLoad = True
            if cube_version == 'cube_20':
                (firmware_path, firmware_version) = find_firmware(firmware_root_3dr)
            elif cube_version == 'cube_21':
                (firmware_path, firmware_version) = find_firmware(firmware_root_green)
        else:
            # Not a clean install, look for FW in the normal location
            (firmware_path, firmware_version) = find_firmware(firmware_root)

    if firmware_path is None:
        logger.info("no new firmware file")
    elif os.path.exists("/log/.factory") and (baud is not None) and verify_usb():
        logger.info("pixhawk: factory - not loading firmware")
    else:
        print "pixhawk: loading firmware"
        logger.info("%s:", firmware_path)
        for v in firmware_version:
            logger.info("%-20s %s", v, firmware_version[v])
        if load(firmware_path):
            move_firmware(firmware_path)
            os.system("rm -rf /firmware/3dr")
            os.system("rm -rf /firmware/green")
            os.system("rm -rf /firmware/wipe")
            if paramsAfterLoad:
                logger.info("Executing post-install parameter reset...")
                resetParameters()
                rebootPixhawk()
                logger.info("...Reset complete")
        else:
            logger.info("Firmware loading failed. Deleting firmware file from directory")
            removeFWfiles("main")

    os.system("rm -f /log/.factory")

    running_version = get_version()
    logger.info(running_version)

    logger.info("now running:")
    for component in ["build_version", "ardupilot_git_hash", "px4_git_hash", "nuttx_git_hash"]:
        if component in running_version:
            version = running_version[component]
        else:
            version = "unknown"
        logger.info("%-20s %s", component, version)
    write_version_file(running_version)
    if "build_version" in running_version \
        and running_version["build_version"] != "unknown":
        logger.info("pixhawk status: ready")
        print "pixhawk: running %s" % running_version["build_version"]
    else:
        print "pixhawk: ERROR checking version"
    led.off()
    end_us = clock.gettime_us(clock.CLOCK_MONOTONIC)
    logger.debug("pixhawk initialization took %0.3f sec",
                 (end_us - start_us) / 1000000.0)


if __name__ == "__main__":

    from optparse import OptionParser

    parser = OptionParser("pixhawk.py [options]")
    parser.add_option("-i", "--init", dest="init", action="store_true",
                      default=False, help="initialize pixhawk")
    parser.add_option("-v", "--version", dest="version", action="store_true",
                      default=False, help="find pixhawk version")
    parser.add_option("-b", "--baud", dest="baud", action="store_true",
                      default=False, help="find pixhawk baud")
    parser.add_option("-l", "--load", dest="load", type="string",
                      default=None, help="load pixhawk firmware")
    parser.add_option("-u", "--usb", dest="usb", action="store_true",
                      default=False, help="connect usb and see what's there")
    parser.add_option("-c", "--config", dest="conf", type="string", default=None,
                      help="configuration file (%s)" % sololink_conf)
    parser.add_option("-f", "--firmware", dest="firm", type="string", default=None,
                      help="firmware root directory (%s)" % firmware_root)
    parser.add_option("-p", "--pix_version", dest="pix", type="string", default=None,
                      help="pixhawk version file (%s)" % version_file)
    (opts, args) = parser.parse_args()

    if opts.conf is not None:
        sololink_conf = opts.conf

    if opts.firm is not None:
        firmware_root = opts.firm

    if opts.pix is not None:
        version_file = opts.pix

    logging.config.fileConfig(sololink_conf)
    logger = logging.getLogger("pix")

    logger.info("pixhawk.py starting")

    if opts.init:
        # initialize, logging any unexpected exceptions
        try:
            initialize()
        except Exception as ex:
            logger.error("unhandled exception!")
            except_str = traceback.format_exc()
            print except_str
            except_str_list = except_str.split('\n')
            for str in except_str_list:
                logger.error(str)
        # try end

    if opts.version:
        print get_version()

    if opts.baud:
        print get_baud()

    if opts.load is not None:
        load(opts.load)

    if opts.usb:
        print check_usb()

    logger.info("pixhawk.py finished")
