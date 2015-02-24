# -*- coding: utf-8 -*-
"""
An interface to the Beaglebone Black-based Real-Time Controller

Should work with both Python 2 and Python 3

@author: David A.W. Barton (david.barton@bristol.ac.uk)

Copyright (c) 2014, David A.W. Barton (david.barton@bristol.ac.uk)
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""


import usb.core  # Import pyusb
import usb.util
import struct
import numpy as np
from array import array
import threading
from warnings import warn
from time import sleep

# Maximum USB packet size
MAX_PACKET_SIZE = 64

# Commands that can be sent
CMD_GET_PAR_NAMES = 10
CMD_GET_PAR_SIZES = 11
CMD_GET_PAR_TYPES = 12
CMD_GET_PAR_VALUE = 20
CMD_SET_PAR_VALUE = 25
CMD_GET_STREAM    = 30
CMD_SOFT_RESET    = 90

# Acknowledgement
CMD_ACK = struct.unpack("<I", b'\xFF\xAB\xCD\xFF')[0]

# Stream state
STREAM_STATE_INACTIVE = 0
STREAM_STATE_ACTIVE   = 1
STREAM_STATE_FINISHED = 2

# CRC set-up
CRC_INITIAL = 0xFFFFFFFF

# USB device lock
usblock = threading.Lock()


## rtc_exception - base class for RTC device errors
class rtc_exception(Exception):
    pass


## rtc_parameters - access parameters on the device
class rtc_parameters(object):
    """A simple class to simplify accesses to parameters"""

    _par_list = []

    def __init__(self, rt):
        self._rt = rt
        # To enable tab completion in IPython
        for name in rt.par_id:
            if not name in ["_rt", "_par_list"]:
                setattr(self, name, -1)
                self._par_list.append(name)

    def __getattribute__(self, name):
        super_self = super(rtc_parameters, self)
        if name in super_self.__getattribute__("_par_list"):
            return super_self.__getattribute__("_rt").get_par(name)
        else:
            return super_self.__getattribute__(name)

    def __setattr__(self, name, value):
        if name in self._par_list:
            return self._rt.set_par(name, value)
        else:
            return super(rtc_parameters, self).__setattr__(name, value)


## rtc_interface - main device interface
class rtc_interface(object):
    """Interface to the real-time control device. As far as possible this
    object is designed to be state-less. (The exception being the parameter
    list.)"""

    def __init__(self):
        # Get the device
        self.dev = usb.core.find(idVendor=0x0123, idProduct=0x4567)
        if self.dev is None:
            raise rtc_exception("RTC device not found")

        # Set the end points and interface of interest
        self.intf = 0
        self.ep_out = 1
        self.ep_in = 129

        # The timeout for any USB communications
        self.timeout = 5000

        # Get the parameter names
        self.get_par_list()

        # A helper class for getting/setting parameters
        self.par = rtc_parameters(self)

    def send_raw(self, data):
        """Send raw data to the RTC device"""
        assert self.dev.write(self.ep_out, data, self.intf, self.timeout) == len(data)

    def send_cmd(self, cmd, data=None):
        """Send a command to the RTC device"""
        if (data is None) or (len(data) == 0):
            self.send_raw(struct.pack("<HI", cmd, 0))
        else:
            if type(data) is bytes:
                array_data = array('B')
                array_data.fromstring(data)
                crc = crc_final(crc_update(CRC_INITIAL, array_data))
                self.send_raw(struct.pack("<HI%dsI" % len(data), cmd, len(data), data, crc))
            elif type(data) is array:
                bytes_data = data.tostring()
                crc = crc_final(crc_update(CRC_INITIAL, data))
                self.send_raw(struct.pack("<HI%dsI" % len(bytes_data), cmd, len(data), bytes_data, crc))

    def recv_raw(self, nbytes):
        """Read raw data from the RTC device"""
        if nbytes % MAX_PACKET_SIZE != 0:
            warn("recv_raw should be used with a multiple of MAX_PACKET_SIZE bytes to avoid potential data corruption")
        return self.dev.read(self.ep_in, nbytes, self.intf, self.timeout)

    def recv_cmd(self):
        """Read the return value from a command from the RTC device"""
        data = self.recv_raw(MAX_PACKET_SIZE)
        if len(data) < 4:
            raise rtc_exception("No intelligible response from RTC device")
        else:
            # Check for a simple acknowledgement response
            if struct.unpack("<I", data)[0] == CMD_ACK:
                return True
            # Check how much data is needed
            data_len = struct.unpack("<I", data[0:4])[0]
            while data_len + 8 > len(data):
                # Need to get more data
                data_needed = data_len + 8 - len(data)
                if data_needed % MAX_PACKET_SIZE != 0:
                    data_needed += MAX_PACKET_SIZE - (data_needed % MAX_PACKET_SIZE)
                new_data = self.recv_raw(data_needed)
                if len(new_data) == 0:
                    raise rtc_exception("Timed out while waiting for data")
                data = data + new_data
            # Process the data
            output = data[4:data_len+4]
            output_crc = struct.unpack("<I", data[data_len+4:data_len+8])[0]
            crc = crc_final(crc_update(CRC_INITIAL, output))
            if crc != output_crc:
                raise rtc_exception("Error in data stream returned from RTC device - CRC failed")
            else:
                return output

    def release_usb(self):
        """Release the USB interface to the RTC device"""
        usb.util.release_interface(self.dev, self.intf)

    def get_par_list(self):
        """Get the parameters available to access on the RTC device"""
        type_sizes = {'b' : 1, 'h' : 2, 'i' : 4, 'B' : 1, 'H' : 2, 'I' : 4, 'c' : 1, 'f' : 4}
        with usblock:
            self.send_cmd(CMD_GET_PAR_NAMES)
            names = self.recv_cmd().tostring()[0:-1].split(b"\0")
            self.send_cmd(CMD_GET_PAR_SIZES)
            sizes = struct.unpack("<%dI" % len(names), self.recv_cmd())
            self.send_cmd(CMD_GET_PAR_TYPES)
            types = struct.unpack("<%dc" % len(names), self.recv_cmd())
        self.par_info = []
        self.par_id = {}
        for i in range(0, len(names)):
            if type(names[i]) is str:
                par_name = names[i]
                par_type = types[i]
            else:
                par_name = names[i].decode()
                par_type = types[i].decode()
            par_info = {"id" : i,
                        "name" : par_name,
                        "type" : par_type,
                        "size" : sizes[i],
                        "count" : sizes[i]/type_sizes[par_type]}
            self.par_info.append(par_info)
            self.par_id[par_name] = i

    def set_par(self, names, values):
        """Set the values of particular parameters on the RTC device"""
        if type(names) is str:
            names = [names]
            values = [values]
        # Pack the data appropriately
        payload = b''
        for (name, value) in zip(names, values):
            par_id = self.par_id[name]
            par_type = self.par_info[par_id]["type"]
            par_count = self.par_info[par_id]["count"]
            try:
                payload += struct.pack("<I%d%s" % (par_count, par_type), par_id, *value)
            except:
                payload += struct.pack("<I%d%s" % (par_count, par_type), par_id, value)
        # Send the command
        with usblock:
            self.send_cmd(CMD_SET_PAR_VALUE, payload)
            if self.recv_cmd():
                return True
            else:
                raise rtc_exception("Failed to set parameter values")

    def get_par(self, names):
        """Get the values of particular parameters from the RTC device"""
        if type(names) is str:
            names = [names]
            islist = False
        else:
            islist = True
        # Get the ids associated with the names provided
        par_ids = []
        for name in names:
            assert(name in self.par_id)
            par_ids.append(self.par_id[name])
        # Request the parameter values
        with usblock:
            self.send_cmd(CMD_GET_PAR_VALUE, struct.pack("<%dI" % len(par_ids), *par_ids))
            data = self.recv_cmd()
        # Reformat the data as appropriate
        idx = 0
        data_list = []
        for par_id in par_ids:
            par_type = self.par_info[par_id]["type"]
            par_count = self.par_info[par_id]["count"]
            par_size = self.par_info[par_id]["size"]
            if par_count > 1:
                data_list.append(array(par_type,
                                       struct.unpack("<%d%s" % (par_count, par_type),
                                                     data[idx:idx+par_size])))
            else:
                data_list.append(struct.unpack("<%d%s" % (par_count, par_type),
                                               data[idx:idx+par_size])[0])
            idx += par_size
        # Return as a list or raw data depending on what was passed originally
        if islist:
            return data_list
        else:
            return data_list[0]

    def set_stream(self, stream, parameters=None, samples=None, downsample=None):
        """Set stream parameters, sample count and downsample rate"""
        stream_name = "S%d" % stream
        if samples is not None:
            assert(type(samples) is int)
            self.set_par(stream_name + "samples", samples)
        if downsample is not None:
            assert(type(downsample) is int)
            self.set_par(stream_name + "downsample", downsample)
        if parameters is not None:
            stream_par = stream_name + "params"
            npars = self.par_info[self.par_id[stream_par]]["count"]
            par_ids = []
            for name in parameters:
                par_id = self.par_id[name]
                par_ids.append(par_id)
                if not self.par_info[par_id]["type"] in ["i", "I", "f"]:
                    raise rtc_exception("Stream parameters must be either floats or ints")
            while len(par_ids) < npars:
                par_ids.append(0xFFFFFFFF)
            assert(len(par_ids) == npars)
            self.set_par(stream_par, par_ids)

    def get_stream(self, stream):
        """Get stream data from a finished stream"""
        stream_name = "S%d" % stream
        if self.get_par(stream_name + "state") != STREAM_STATE_FINISHED:
            return None
        par_ids = self.get_par(stream_name + "params")
        par_count = 0
        for i in range(0, len(par_ids)):
            if par_ids[i] >= len(self.par_id):
                break
            par_count = i + 1
        par_ids = par_ids[0:par_count]
        par_types = "<"
        par_names = []
        data = {}
        for par_id in par_ids:
            par_type = self.par_info[par_id]["type"]
            par_types += par_type
            par_name = self.par_info[par_id]["name"]
            par_names.append(par_name)
            data[par_name] = array(par_type)
        self.send_cmd(CMD_GET_STREAM, struct.pack("<I", stream))
        raw_data = self.recv_cmd()
        idx = 0
        while idx < len(raw_data):
            tmp_data = struct.unpack(par_types, raw_data[idx:idx + par_count*4])
            for i in range(0, par_count):
                data[par_names[i]].append(tmp_data[i])
            idx += par_count*4
        return data

    def start_stream(self, stream):
        """Start a stream recording data"""
        stream_name = "S%d" % stream
        self.set_par(stream_name + "state", STREAM_STATE_ACTIVE)
        if self.get_par(stream_name + "state") != STREAM_STATE_INACTIVE:
            return True
        else:
            return False

    def run_stream(self, stream, start=True, wait_period=0.1):
        """Start a stream recording data and return the resulting data"""
        stream_name = "S%d" % stream
        if start:
            if not self.start_stream(stream):
                raise rtc_exception("Failed to start stream - perhaps bad parameters")
        elif self.get_par(stream_name + "state") == STREAM_STATE_INACTIVE:
            raise rtc_exception("Stream not already started")
        while self.get_par(stream_name + "state") == STREAM_STATE_ACTIVE:
            sleep(wait_period)
        return self.get_stream(stream)


## CRC tables - this is the same as used in the PNG standard
crc_table = np.zeros(256, dtype=np.uint32)

def crc_setup():
    """Set up the CRC tables"""
    for n in np.arange(0, 256, dtype=np.uint32):
        c = n
        for k in np.arange(0, 8, dtype=np.uint32):
            if c & 1:
                c = 0xEDB88320 ^ (c >> 1)
            else:
                c = c >> 1
        crc_table[n] = c

def crc_update(crc, data):
    """Update the CRC with the supplied data; use crc=CRC_INITIAL to start"""
    assert(type(data) is array)
    for i in range(0, len(data)):
        crc = crc_table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8)
    return crc

def crc_final(crc):
    """Finalise the CRC"""
    return crc ^ 0xFFFFFFFF

crc_setup()