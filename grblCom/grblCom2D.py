# -*- coding: UTF-8 -*-

'''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''
'                                                                         '
' Copyright 2018 Gauthier Brière (gauthier.briere "at" gmail.com)         '
'                                                                         '
' This file is part of cn5X                                               '
'                                                                         '
' cn5X is free software: you can redistribute it and/or modify it         '
'  under the terms of the GNU General Public License as published by      '
' the Free Software Foundation, either version 3 of the License, or       '
' (at your option) any later version.                                     '
'                                                                         '
' cn5X is distributed in the hope that it will be useful, but             '
' WITHOUT ANY WARRANTY; without even the implied warranty of              '
' MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           '
' GNU General Public License for more details.                            '
'                                                                         '
' You should have received a copy of the GNU General Public License       '
' along with this program.  If not, see <http://www.gnu.org/licenses/>.   '
'                                                                         '
'''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''

import sys, time
from enum import Enum
from math import *
from PyQt5.QtCore import QCoreApplication, QObject, QThread, QTimer, QEventLoop, pyqtSignal, pyqtSlot, QIODevice
from PyQt5.QtSerialPort import QSerialPort, QSerialPortInfo
from grblComStack import gcodeStack


class logSeverity(Enum):
  info    = 0
  warning = 1
  error =   2


class grblSerialCom(QObject):
  ''' 
  QObject worker assurant la gestion de la communication série  
  bidirectionnelle entre cn5X et grbl.
  '''

  sig_log    = pyqtSignal(int, str) # Message de fonctionnement du composant grblSerialCom, renvoie : logSeverity, message string
  sig_init   = pyqtSignal(str)      # Emis à la réception de la chaine d'initialisation de Grbl, renvoie la chaine complète
  sig_ok     = pyqtSignal()         # Emis à la réception de la chaine "ok"
  sig_error  = pyqtSignal(int)      # Emis à la réception d'une erreur Grbl, renvoie le N° d'erreur
  sig_alarm  = pyqtSignal(int)      # Emis à la réception d'une alarme Grbl, renvoie le N° d'alarme
  sig_status = pyqtSignal(str)      # Emis à la réception d'un message de status ("<...|.>"), renvoie la ligne complète
  sig_data   = pyqtSignal(str)      # Emis à la réception des autres données de Grbl, renvoie la ligne complète
  sig_emit   = pyqtSignal(str)      # Emis à l'envoi des données sur le port série 
  sig_debug  = pyqtSignal(str)      # Emis à chaque envoi ou réception

  def __init__(self, comPort: str, baudRate: int):
    super().__init__()
    self.__abort = False
    self.__portName = comPort
    self.__baudRate = baudRate
    self.__comPort  = QSerialPort()
    self.__comPort.setPortName(self.__portName)
    self.__comPort.setBaudRate(self.__baudRate)
    self.__comPort.setDataBits(QSerialPort.Data8)
    self.__comPort.setStopBits(QSerialPort.OneStop)
    self.__comPort.setParity(QSerialPort.NoParity)
    
    self.__realTimeStack = gcodeStack()
    self.__mainStack     = gcodeStack()

  def run(self):

    thread_name = QThread.currentThread().objectName()
    thread_id = int(QThread.currentThreadId())  # cast to int() is necessary
    self.sig_log.emit(logSeverity.info, 'grblSerialCom running "{}" from thread #{}.'.format(thread_name, hex(thread_id)))

    # Ouverture du port série
    RC = False
    try:
      RC = self.__comPort.open(QIODevice.ReadWrite)
    except OSError as err:
      self.sig_log.emit(logSeverity.error, "grblSerialCom : Erreur ouverture du port : {0}".format(err))
      return False
    except:
      self.sig_log.emit(logSeverity.error, "grblSerialCom : Unexpected error : {}".format(sys.exc_info()[0]))
      return False
    if not RC:
      self.sig_log.emit(logSeverity.error, "grblSerialCom : Erreur à l'ouverture du port série : err# = {0}".format(self.__comPort.error()))
      return False

    self.sig_log.emit(logSeverity.info, "grblSerialCom : comPort {} ouvert".format(self.__comPort.portName()))

    # Boucle principale du composant
    while 1:
      # On commence par vider la file d'attente des commandes temps réel
      while not self.__realTimeStack.isEmpty():
        toSend = self.__realTimeStack.pop()
        __sendData(toSend)
      # Ensuite, on envoie une ligne gcode en attente
      if not self.__mainStack.isEmpty():
        toSend = self.__mainStack.pop()
        if toSend[-1:] != '\n':
          toSend += '\n'
          __sendData(toSend)
          # Boucle de lecture du port série
          serialData   = ''
          foundErrorOk = False
          while 1:
            if self.__comPort.waitForReadyRead(50):
              buff = self.__comPort.readAll()
              try:
                serialData += buff.data().decode()
              except:
                self.sig_log.emit(logSeverity.error, "grblSerialCom : Erreur décodage : {}".format(sys.exc_info()[0]))
                serialData = ''
              if serialData != '':
                # Découpe les données reçues en lignes pour les envoyer une par une
                tblLines = serialData.splitlines()
                if serialData[-1] != "\n":
                  # La dernière ligne est incomplette, on envoi jusqu'à l'avant dernière.
                  for l in tblLines[:-1]:
                    if l.find('ok') >= 0 or l.find('error') >= 0: foundErrorOk = True
                    self.traileLaLigne(l)
                  # On laisse la derniere ligne dans le buffer pour qu'elle soit complettée.
                  serialData = tblLines[-1:]
                else:
                  # La dernière ligne est complette, on envoi tout
                  for l in tblLines:
                    if l.find('ok') >= 0 or l.find('error') >= 0: foundErrorOk = True
                    self.traileLaLigne(l)
                  serialData=''
                  if foundErrorOk:
                    # On à eu la réponse de Grbl, on peux passer à la suite.
                    break
            # Process events to receive signals;
            QCoreApplication.processEvents()
            if self.__abort:
              self.sig_log.emit(logSeverity.warning, "grblSerialCom : aborting while waiting for Grbl response...")
              break # Sortie de la boucle de lecture
            # On a pas fini d'attendre la réponse de Grbl, on traite le temps réel s'il y en a en attente
            while not self.__realTimeStack.isEmpty():
              toSend = self.__realTimeStack.pop()
              __sendData(toSend)

      if self.__abort:
        self.sig_log.emit(logSeverity.info, "grblSerialCom : quitting...")
        break # Sortie de la boucle principale

    # Sortie de la boucle principale
    self.sig_log.emit(logSeverity.info, "grblSerialCom : Fermeture du port série.")
    self.__comPort.close()
    # Emission du signal de fin
    self.sig_log.emit(logSeverity.info, "grblSerialCom : End.")


def __sendData(self, buff: str):
  ''' Envoie des données sur le port série '''
  
  # Envoi de toutes les lignes dans le debug
  if buff[-1:] == "\n":
    self.sig_debug.emit(">>> " + buff[:-1] + "\\n")
  elif buff[-2:] == "\r\n":
    self.sig_debug.emit(">>> " + buff[:-2] + "\\r\\n")
  else:
    self.sig_debug.emit(">>> " + buff)
  # Formatage du buffer à envoyer
  buffWrite = bytes(buff, sys.getdefaultencoding())
  # Temps nécessaire pour la com (millisecondes), arrondi à l'entier supérieur
  tempNecessaire = ceil(1000 * len(buffWrite) * 8 / self.__baudRate) 
  timeout = 10 + (2 * tempNecessaire) # 2 fois le temps nécessaire + 10 millisecondes
  # Ecriture sur le port série
  self.__comPort.write(buffWrite)
  if self.__comPort.waitForBytesWritten(timeout):
    self.sig_emit.emit(buff)
  else:
    self.sig_log.emit(logSeverity.error, "grblSerialCom : Erreur envoi des données : timeout, err# = {0}".format(self.__comPort.error()))


  def traileLaLigne(self, l):
    ''' Emmet les signaux ad-hoc pour toutes les lignes reçues  '''

    # Envoi de toutes les lignes dans le debug
    if l[-1:] == "\n":
      self.sig_debug.emit(">>> " + l[:-1] + "\\n")
    elif l[-2:] == "\r\n":
      self.sig_debug.emit(">>> " + l[:-2] + "\\r\\n")
    else:
      self.sig_debug.emit(">>> " + l)
    # Premier décodage pour envoyer le signal ah-hoc
    if l[:5] == "Grbl " and l[-5:] == "help]": # Init string : Grbl 1.1f ['$' for help]
      self.sig_init.emit(l)
    elif l == "ok":                            # Reponses "ok"
      self.sig_ok.emit()
    elif l[:6] == "error:":                    # "error:X" => Renvoie X
      errNum = int(l.split(':')[1])
      self.sig_error.emit(errNum)
    elif l[:6] == "ALARM:":                    # "ALARM:X" => Renvoie X
      alarmNum = int(l.split(':')[1])
      self.sig_alarm.emit(alarmNum)
    elif l[:1] == "<" and l[-1:] == ">":       # Real-time Status Reports
      self.sig_status.emit(l[1:-1])
    else:
      self.sig_data.emit(l)


  @pyqtSlot()
  def abort(self):
    self.sig_msg.emit("serialCommunicator : abort reçu.")
    self.__abort = True
















