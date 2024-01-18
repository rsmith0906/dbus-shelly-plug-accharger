#!/usr/bin/env python

# import normal packages
import platform
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusShellyService:
  def __init__(self, servicename, paths, productname='Shelly Plug AC Charger', connection='Shelly Plug AC Charger HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    customname = config['DEFAULT']['CustomName']

    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
    self._paths = paths

    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)

    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    #self._dbusservice.add_path('/ProductId', 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
    self._dbusservice.add_path('/ProductId', 0xFFFF) # id assigned by Victron Support from SDM630v2.py
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', customname)
    self._dbusservice.add_path('/Connected', 1)

    self._dbusservice.add_path('/Latency', None)
    self._dbusservice.add_path('/FirmwareVersion', self._getShellyFWVersion())
    self._dbusservice.add_path('/HardwareVersion', 0)
    self._dbusservice.add_path('/Position', int(config['DEFAULT']['Position']))
    self._dbusservice.add_path('/Serial', self._getShellySerial())
    self._dbusservice.add_path('/UpdateIndex', 0)
    #self._dbusservice.add_path('/State', 0)  # Dummy path so VRM detects us as a AC Charger.

    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # last update
    self._lastUpdate = 0

    # add _update function 'timer'
    gobject.timeout_add(250, self._update) # pause 250ms before the next request

    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)

  def _getShellySerial(self):
    meter_data = self._getShellyData()

    if not meter_data['mac']:
        raise ValueError("Response does not contain 'mac' attribute")

    serial = meter_data['mac']
    return serial

  def _getShellyFWVersion(self):
    meter_data = self._getShellyData()

    if not meter_data['update']['old_version']:
        raise ValueError("Response does not contain 'update/old_version' attribute")

    ver = meter_data['update']['old_version']
    return ver

  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config;


  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']

    if not value:
        value = 0

    return int(value)


  def _getShellyStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']

    if accessType == 'OnPremise': 
        URL = "http://%s:%s@%s/status" % (config['ONPREMISE']['Username'], config['ONPREMISE']['Password'], config['ONPREMISE']['Host'])
        URL = URL.replace(":@", "")
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

    return URL


  def _getShellyData(self):
    URL = self._getShellyStatusUrl()
    meter_r = requests.get(url = URL)

    # check for response
    if not meter_r:
        raise ConnectionError("No response from Shelly Plug AC Charger - %s" % (URL))

    meter_data = meter_r.json()

    # check for Json
    if not meter_data:
        raise ValueError("Converting response to JSON failed")


    return meter_data


  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._lastUpdate))
    logging.info("Last '/Ac/In/L1/V': %s" % (self._dbusservice['/Ac/In/L1/V']))
    logging.info("--- End: sign of life ---")
    return True

  def _update(self):
    try:
       #get data from Shelly 1pm
       meter_data = self._getShellyData()

       config = self._getConfig()
       str(config['DEFAULT']['Phase'])

       accharger_phase = str(config['DEFAULT']['Phase'])
       accharger_limit = int(config['DEFAULT']['CurrentLimit'])

       #send data to DBus
       for phase in ['L1']:
         pre = '/Ac/In/' + phase

         if phase == accharger_phase:
           power = meter_data['meters'][0]['power']
           total = meter_data['meters'][0]['total']
           current = power / 120

           self._dbusservice['/NrOfOutputs'] = 1
           self._dbusservice['/Dc/0/Voltage'] = 24
           self._dbusservice['/Dc/0/Current'] = 30
           self._dbusservice['/Ac/In/CurrentLimit'] = accharger_limit
           self._dbusservice[pre + '/I'] = current
           self._dbusservice[pre + '/P'] = power
           if power > 0:
             self._dbusservice['/State'] = 3
           else:
             self._dbusservice['/State'] = 0

         else:
           self._dbusservice['/Ac/In/CurrentLimit'] = 0
           self._dbusservice[pre + '/I'] = 0
           self._dbusservice[pre + '/P'] = 0
           self._dbusservice['/State'] = 0
           self._dbusservice['/NrOfOutputs'] = 0

       self._dbusservice['/Ac/In/L1/P'] = self._dbusservice['/Ac/In/' + accharger_phase + '/P']

       #logging
       logging.debug("AC Charger Input (/Ac/In/L1/P): %s" % (self._dbusservice['/Ac/In/L1/P']))
       logging.debug("---");

       # increment UpdateIndex - to show that new data is available
       index = self._dbusservice['/UpdateIndex'] + 1  # increment index
       if index > 255:   # maximum value of the index
         index = 0       # overflow from 255 to 0
       self._dbusservice['/UpdateIndex'] = index

       #update lastupdate vars
       self._lastUpdate = time.time()
    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)

    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True

  def _handlechangedvalue(self, path, value):
    logging.debug("someone else updated %s to %s" % (path, value))
    return True # accept the change



def main():
  #configure logging
  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging.INFO,
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])

  try:
      logging.info("Start");

      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)

      #formatting
      _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
      _state = lambda p, v: (str(v))
      _mode = lambda p, v: (str(v))
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _l = lambda p, v: (str(round(v, 1)) + 'A')
      _outputs = lambda p, v: (str(v))
      _voltage = lambda p, v: (str(round(v, 1)) + 'V')
      _current = lambda p, v: (str(round(v, 1)) + 'A')

      #start our main-service
      pvac_output = DbusShellyService(
        servicename='com.victronenergy.charger',
        paths={
          '/Ac/In/L1/I': {'initial': 0, 'textformat': _a},
          '/Ac/In/L1/P': {'initial': 0, 'textformat': _w},
          '/Ac/In/CurrentLimit': {'initial': 0, 'textformat': _l},
          '/State': {'initial': 0, 'textformat': _state},
          '/Mode': {'initial': 4, 'textformat': _mode},
          '/NrOfOutputs': {'initial': 0, 'textformat': _outputs},
          '/Dc/0/Voltage': {'initial': 0, 'textformat': _voltage},
          '/Dc/0/Current': {'initial': 0, 'textformat': _current},
        })

      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
