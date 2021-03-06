"""
This script reads the SF signal cards (which are in Excel workbooks) and creates corresponding
:py:class:`dta.TimePlan` instances for them.
"""

__copyright__   = "Copyright 2011 SFCTA"
__license__     = """
    This file is part of DTA.

    DTA is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    DTA is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with DTA.  If not, see <http://www.gnu.org/licenses/>.
"""

#import json
import pdb

import csv 
import difflib
from collections import defaultdict
import os
import pickle
import re
import xlrd
from itertools import izip, chain
import sys 

import datetime

from dta.MultiArray import MultiArray

import dta
from dta.DynameqScenario import DynameqScenario
from dta.DynameqNetwork import DynameqNetwork
from dta.Algorithms import pairwise, any2, all2 
from dta.TimePlan import TimePlan, PlanCollectionInfo
from dta.Phase import Phase
from dta.PhaseMovement import PhaseMovement
from dta.Utils import Time

class ExcelCardError(Exception):
    pass

class MovementMappingError(ExcelCardError):
    pass

class SignalConversionError(ExcelCardError):
    pass

class ParsingCardError(ExcelCardError):
    pass

class ExcelSignalTimingError(ParsingCardError):
    pass

class StreetNameMappingError(ExcelCardError):
    pass


GREEN = 0
YELLOW = 1
RED = 2

TURN_LEFT = ("LT", "LT2")
TURN_THRU = ("TH", "TH2")
TURN_RIGHT = ("RT", "RT2")

#: How to use this script
USAGE = r"""

 python importExcelSignals.py dynameq_net_dir dynameq_net_prefix excel_signals_dir startTime endTime output_dynameq_dir output_dynameq_net_prefix [overrideturntypes.csv]
 
 e.g.
 
 python importExcelSignals.py . sf Y:\dta\SanFrancisco\2010\excelSignalCards 15:30 18:30 Y:\dta\SanFrancisco\2010\network\movement_override.csv
 
 This script reads all the excel signal cards in the excel_signal_cards dir and converts them to the dynameq network
 specified with the first two arguments.
 
 Only signals that are active in the given time period are converted.
 
 The script writes a new dynameq network that includes the signals to output_dynameq_dir using the prefix output_dynameq_net_prefix
 """

class SignalData(object):
    """
    Class that represents the signal data for an intersection.
    """

    attrNames = ["fileName", "iName", "topLeftCell", "phaseSeqCell", "pedPhaseCell",\
                              "sigInterCell", "colPhaseData", "lastColPhaseData"] \
                              + ["gMov%d" % i for i in range(1, 9)]
    streets = ["Street0", "Street1", "Street2", "Street3"]

    mappingInfo = ["IntersectionName(for mapping)", "mapped Intersection Name", "mapped Node id"]

    def __init__(self):        

        self.fileName = None
        self.iName = None
        self.iiName = ""
        self.topLeftCell = None
        self.phaseSeqCell = None
        self.pedPhaseCell = None
        self.sigInterCell = None
        self.colPhaseData = None
        self.lastColPhaseData = None
        self.streetNames = []
        self.mappedNodeName = ""
        self.mappedNodeId = ""
        self.mappedNode = None

        self.mappedStreet = {}
        self.mappedMovements = defaultdict(list)  #indexed by the keys of the mappedStreet 
        self.signalTiming = {} # signal timing objects indexed by cso
        
        self.error = None

        #the group movement names
        self.gMov1 = ""
        self.gMov2 = ""
        self.gMov3 = ""
        self.gMov4 = ""
        self.gMov5 = ""
        self.gMov6 = ""
        self.gMov7 = ""
        self.gMov8 = ""

        self.phasingData = None
        self.sIntervals = {}

    def toDict(self):

        result = {}
        result["fileName"] = self.fileName
        result["intersectionName"] = self.iName
        result["mappedNodeName"] = self.mappedNodeName
        result["mappedNodeId"] = self.mappedNodeId
        
        result["phasing"] = {}
        result["phasing"]["phaseNumbers"] = self.phasingData.getElementsOfDimention(1)
        for streetName in self.phasingData.getElementsOfDimention(0):
            tmp = []
            for phaseElem in self.phasingData[streetName, :]:
                tmp.append(phaseElem)
            result["phasing"][streetName] = tmp
        
        result["timeOfDayPhasing"] = {}
        for key, value in self.signalTiming.iteritems():
            
            result["timeOfDayPhasing"][key] = value.__dict__
        return result 
        
    def __repr__(self):
        
        str1 = "\t".join([str(getattr(self, attr)) for attr in SignalData.attrNames])
        streetNames = ["",] * 4
        for i, name in enumerate(self.streetNames):
            streetNames[i] = name
        str2 = "\t".join(streetNames)
        return str1 + "\t" + str2 + "\t" + self.iiName + "\t" + self.mappedNodeName + "\t" + self.mappedNodeId

    def __str__(self):
        
        result = "\n\n%30s=%30s\n%30s=%30s\n%30s=%30s" % ("FileName", 
                     self.fileName, "Intersection Name", 
                       self.iName, "Internal Intersection Name", self.iiName)

        result += "\n%30s=%30s\n%30s=%30s" % ("mappedNodeName", self.mappedNodeName, "mappedNodeId", self.mappedNodeId)
        
        result += "\n\nPhasing Data\n"
        result += self.phasingData.asPrettyString()
        
        result += "\nTiming Data\n"
        for signalTiming in self.signalTiming.values():
            result += str(signalTiming)
        return result 

    def iterSignalTiming(self):
        """
        Return an iterator to the signal Timing objects
        """
        return iter(self.signalTiming.values())
    
    def getNumTimeIntervals(self):
        """
        Return the number of time intervals
        """
        if self.signalTiming:
            return len(self.signalTiming.values()[0])

    def getPhases(self, startHour, endHour):
        """
        Get the phases as list of dictionaries. By combining phasing data information
        such as the one in the following table 
        
        ::
        
         Phasing Data
                            1  2  3  4  5  6  7  8
           GEARY BLVD (EB)  G  G  Y  R  R  R  R  R
             10TH AVE (SB)  R  R  R  R  G  G  Y  R
           GEARY BLVD (WB)  G  G  Y  R  R  R  R  R
             10TH AVE (NB)  R  R  R  R  G  G  Y  R

        And the signal interval data for the time of day that is specified
        by the startHour and endHour input arguments
 
	    Times: [47.0, 6.0, 3.5, 0.5, 8.0, 20.0, 3.5, 1.5]        
        """

        pPhase = {}
        cPhase = {}
        phases = []
        allRed = 0
        
        phasingData = self.phasingData
        groupMovements = phasingData.getElementsOfDimention(0)
        timeIndices = phasingData.getElementsOfDimention(1)
        timeIntervals = selectCSO(self, startHour, endHour).times
        lastIndex = list(timeIndices)[-1]

        for timeIndex1, dur1 in izip(timeIndices, timeIntervals):
            try:
                states = list(iter(phasingData[:, timeIndex1]))
            except ValueError, e:
                print e
                raise SignalConversionError(str(e))
            
            cGreenMovs = [gMov for gMov in groupMovements if phasingData[gMov, timeIndex1] == "G"] 
            cYellowMovs = [gMov for gMov in groupMovements if phasingData[gMov, timeIndex1] == "Y"]
            cRedMovs = [gMov for gMov in groupMovements if phasingData[gMov, timeIndex1] == "R"]

            gMatches = 0
            yMatches = 0
            rMatches = 0
            if pPhase:
                for gMovs in cGreenMovs:
                    if gMovs in pPhase["Movs"]:
                        gMatches += 1
                for yMovs in cYellowMovs:
                    if yMovs in pPhase["Movs"]:
                        yMatches += 1
                for rMovs in cRedMovs:
                    if rMovs in pPhase["Movs"]:
                        rMatches += 1
                            
            else:
                pPhase["Movs"] = cGreenMovs
                pPhase["green"] = dur1
                pPhase["yellow"] = 0
                pPhase["allRed"] = 0
                continue

            if len(cGreenMovs) + len(cYellowMovs) > (gMatches + yMatches):
                phases.append(pPhase)
                pPhase = {}
                pPhase["Movs"] = cGreenMovs
                pPhase["green"] = dur1
                pPhase["yellow"] = 0
                pPhase["allRed"] = 0

            elif gMatches + yMatches == len(pPhase["Movs"]):
                if yMatches > 0:
                    pPhase["yellow"] += dur1
                elif rMatches > 0:
                    pPhase["allRed"] += dur1
                else:
                    pPhase["green"] += dur1
            elif gMatches + yMatches < len(pPhase["Movs"]) and gMatches + yMatches > 0:
                phases.append(pPhase)
                pPhase = {}
                if yMatches == 0:
                    pPhase["Movs"] = cGreenMovs
                    pPhase["green"] = dur1
                    pPhase["yellow"] = 0
                    pPhase["allRed"] = 0
                else:
                    pPhase["Movs"] = cYellowMovs
                    pPhase["green"] = 0
                    pPhase["yellow"] = dur1
                    pPhase["allRed"] = 0
            elif rMatches == len(pPhase["Movs"]) and not cGreenMovs:
                pPhase["allRed"] += dur1
            elif rMatches == len(pPhase["Movs"]) and cGreenMovs:
                phases.append(pPhase)
                pPhase = {}
                pPhase["Movs"] = cGreenMovs
                pPhase["green"] = dur1
                pPhase["yellow"] = 0
                pPhase["allRed"] = 0
                
            else:
                phases.append(pPhase)
                pPhase = {}
                pPhase["Movs"] = cActiveMovs
                pPhase["green"] = dur1
                pPhase["yellow"] = 0
                pPhase["allRed"] = 0

            if timeIndex1 == lastIndex:
                phases.append(pPhase)
            
        return phases

    def selectCSO(self, startTime, endTime):
        """
        returns the ExcelSignalTiming if there is one that is in operation during the 
        input hours. Otherwise it returns none
        """
        for cso, signalTiming in self.signalTiming.iteritems(): 
            if startTime >= signalTiming.startTime and startTime <= signalTiming.endTime:
                return cso

        for cso, signalTiming in self.signalTiming.iteritems(): 
            if signalTiming.startTime == dta.Time(0,0) and signalTiming.endTime == dta.Time(23,59):
                return cso
        for cso, signalTiming in self.signalTiming.iteritems(): 
            if signalTiming.startTime == dta.Time(0,0) and signalTiming.endTime == dta.Time(0,0):
                return cso
        return None

class PhaseState(object):
    """Represents the state of a phase for a specfic duration
    The state can be one of GREEN, YELLOW and RED:
    and the duration is a floating number
    """
    GREEN = 0
    YELLOW = 1
    RED = 2    

    def __init__(self, state, duration):
        self.state = state 
        self.duration = duration

class ExcelSignalTiming(object):
    """Contains the timing information for a particular period

    the timing consists of a list of floating numbers that each of which 
    corresponds to a different state of the timing plan

    """
    DEFAULT_VALUE = -1
    DEFAULT_OFFSET = 0

    def __init__(self):

        self.cso = ExcelSignalTiming.DEFAULT_VALUE   # string the cso value
        self.cycle = ExcelSignalTiming.DEFAULT_VALUE  #float the cycle length 
        self.offset = ExcelSignalTiming.DEFAULT_OFFSET  # float the offset

        self.isActuated = False

        self.startTime = dta.Time(23,59)
        self.endTime = dta.Time(23,59)

        self.times = ()
        
    def setPhaseTimes(self, times):        

        for time in times:
            if not isinstance(time, float):
                raise ExcelSignalTimingError("Time values corresponding to phase states have to be float "
                                       "values and not: %s" % str(time))

        self.times = times

    def __iter__(self):
        """
        Return an iterator over the phase states
        """
        return iter(self.state)

    def __len__(self):
        """
        Return the number of intevals of the time plan
        """
        return self.getNumIntervals()

    def getNumIntervals(self):
        """
        Return the number of intevals of the time plan
        """
        return len(self.times)

    def __str__(self):
        """
        Return the string representation of the signal card
        """
        return self.__repr__()

    def __repr__(self):
        """
        Return the string representation of the signal card
        """
        if self.cycle == None:
            cycle = ExcelSignalTiming.DEFAULT_VALUE
        else:
            cycle = self.cycle 

        if self.offset == None:
            offset = ExcelSignalTiming.DEFAULT_VALUE
        else:
            offset = self.offset
        
        result = "\ncso= %s \ncycle= %.1f \noffset= %.1f \nstartTime= %s " \
            "endtime= %s \nisActuated %s" % (self.cso, cycle, offset,
                                          str(self.startTime), str(self.endTime), self.isActuated)
        result += "\n\tTimes: %s" % str(self.times)

        return result
                                                  
def findTopLeftCell(sheet):
    """Find the top left cell of the signal card containing 
    the intersection name and return its coordinates"""

    MAX_RANGE = 10 # max range to look for the top left cell 
    for i in range(MAX_RANGE):
        for j in range(MAX_RANGE):
            if str(sheet.cell_value(i, j)).strip():
                return (i, j)

    raise ParsingCardError("I cannot find top left cell of the sheet containing "
                           "the intersection name")

        
def getIntersectionName(sheet, topLeftCell):
    """
    Return the intersection name as found in the excel card. 
    TopLeftCell is a tuble that contains the location of the top left cell
    """
    return str(sheet.cell_value(topLeftCell[0], topLeftCell[1])).upper()

def findStreet(sheet, topLeftCell):
    """Find the cell containing the STREET keyword that marks
    the beggining of the phase sequencing information and return 
    its coordinates. If unsuccesfull raiseParsingCardError"""
    CELLS_TO_SEARCH = 40
    START_PHASE_SECTION = "STREET"
    for row in range(CELLS_TO_SEARCH):
        cell = (topLeftCell[0] + row, topLeftCell[1])
        try:
            value = str(sheet.cell_value(rowx=cell[0], colx=cell[1]))
        except IndexError:
            return None
        if value.upper().strip().startswith(START_PHASE_SECTION):
            return cell
    raise ParsingCardError("I cannot find the start of the phasing section")

def findPedestrianPhase(sheet, topLeftCell):
    """
    Find the cell that corresponds to the pedestrian phase.
    If not such cell exists it returns None
    """
    CELLS_TO_SEARCH = 60
    for row in range(CELLS_TO_SEARCH):
        cell = (topLeftCell[0] + row, topLeftCell[1])
        try:
            value = str(sheet.cell_value(rowx=cell[0], colx=cell[1]))
        except IndexError:
            return None
        if "PEDS " in value.upper().strip() or \
                "PED " in value.upper().strip() or \
                "XING " in value.upper().strip() or \
                "MUNI " in value.upper().strip() or \
                "TRAIN " in value.upper().strip() or\
                "FLASHING " in value.upper().strip():
            return cell
    return None    

def findSignalIntervals(sheet, topLeftCell):
    """
    Find the cell that contains the Cycle and Offset infrmation
    """

    CELLS_TO_SEARCH = 80
    for row in range(CELLS_TO_SEARCH):
        cell = (topLeftCell[0] + row, topLeftCell[1])
        try:
            value = str(sheet.cell_value(rowx=cell[0], colx=cell[1]))
        except IndexError:
            return None
        if "CSO" in value.upper().strip() or "DIAL" in value.upper().strip():
            return cell
    raise ParsingCardError("I cannot find the start of the singal interval field "
                           "marked by the keyworkd CSO or DIAL")

def getFirstColumnOfPhasingData(sheet, signalData):
    """
    Search the cells in the spreadsheet to find and return the cell that
    contains phase state 1
    """
    if not signalData.phaseSeqCell:
        raise ParsingCardError("I cannot locate the first column of the phasing data "
                               "because the phase data section has not been previously identified")
    
    NUM_COL_TO_SEARCH = 10
    startX, startY = signalData.phaseSeqCell
    for col in range (startY + 1, startY + NUM_COL_TO_SEARCH):
        if sheet.cell_value(rowx=startX, colx=col) == 1:  # one is the first phase
            signalData.colPhaseData = col
            return
    raise ParsingCardError("I cannot locate the first column of the phasing data "
                           "identified by the keyword 1 in the same row with the "
                           "STREET keyword")

def checkForWeekdayPlan(sheet, row, column):
    """
    Checks the plan data to see if the CSO is used on weekdays (Thursday and Friday) 
    """
    ThursValue = str(sheet.cell_value(row,column-3)).strip()
    FriValue = str(sheet.cell_value(row,column-2)).strip()
    if ThursValue=="X" and FriValue=="X":
        return True
    elif ThursValue=="x" and FriValue=="x":
        return True
    else:
        return False
    
def getOperationTimes(sheet, signalData):


    dta.DtaLogger.debug("I am parsing the operating times for signal %s" % signalData.fileName)
                       
    i = signalData.topLeftCell[0] + 12
    j = signalData.colPhaseData

    def setTimes(row,colCycle,sheet):
        hasAllOtherTimes = False
        gotStartTime = False
        gotStartTime2=False

        
        for curColumn in range(signalData.topLeftCell[1], signalData.colPhaseData):
            cellValue = sheet.cell_value(row, curColumn)
            if str(cellValue).strip():
                # Check to see if CSO applies to weekdays and if not, set times to 23:59 to 23:59
                isWeekdayPlan = checkForWeekdayPlan(sheet,row,colCycle)
                if not isWeekdayPlan:
                    signalData.signalTiming[strCso].startTime = dta.Time(23,59)
                    signalData.signalTiming[strCso].endTime = dta.Time(23,59)
                    gotStartTime = True
                # If CSO does include weekdays, check time format and get signal times
                elif isinstance(cellValue, float):
                    if cellValue == 1.0:
                        time = (0, 0, 0, 0, 0, 0)
                    else:
                        time = xlrd.xldate_as_tuple(cellValue, 0)
                    if gotStartTime == False:
                        signalData.signalTiming[strCso].startTime = dta.Time(time[3], time[4])
                        gotStartTime = True
                    else:
                        signalData.signalTiming[strCso].endTime = dta.Time(time[3], time[4])
                elif "event" in cellValue or "Event" in cellValue or "Game" in cellValue:
                    signalData.signalTiming[strCso].startTime = dta.Time(23,59)
                    signalData.signalTiming[strCso].endTime = dta.Time(23,59)
                elif "TIMES" in cellValue or "Times" in cellValue:
                    hasAllOtherTimes = True
                    signalData.signalTiming[strCso].startTime = dta.Time(0,0)
                    signalData.signalTiming[strCso].endTime = dta.Time(0,0)
                    gotStartTime = True
                    
                elif gotStartTime == False:
                    if "-" in cellValue and not ":" in cellValue and not " - " in cellValue:
                        cellValue = cellValue.strip()
                        startTime1 = int(cellValue[:2])
                        startTime2 = int(cellValue[2:4])
                        endTime1 = int(cellValue[5:7])
                        endTime2 = int(cellValue[7:])
                        signalData.signalTiming[strCso].startTime = dta.Time(startTime1, startTime2)
                        signalData.signalTiming[strCso].endTime = dta.Time(endTime1, endTime2)
                        gotStartTime = True
                    if " - " in cellValue and not ":" in cellValue:
                        cellValue = cellValue.strip()
                        startTime1 = int(cellValue[:2])
                        startTime2 = int(cellValue[2:4])
                        endTime1 = int(cellValue[7:9])
                        endTime2 = int(cellValue[9:])
                        if endTime1 == 24:
                            endTime1 = 23
                            endTime2 = 59
                        signalData.signalTiming[strCso].startTime = dta.Time(startTime1, startTime2)
                        signalData.signalTiming[strCso].endTime = dta.Time(endTime1, endTime2)
                        gotStartTime = True
                    if "-" in cellValue and ":" in cellValue:
                        stopval = cellValue.find("-")
                        startTimeAll = cellValue[:stopval]
                        colonval = startTimeAll.find(":")
                        startTime1 = int(startTimeAll[:colonval])
                        startTime2 = int(startTimeAll[colonval+1:])
                        signalData.signalTiming[strCso].startTime = dta.Time(startTime1, startTime2)
                        endTimeAll = cellValue[stopval+1:]
                        colonval = endTimeAll.find(":")
                        endTime1 = int(endTimeAll[:colonval])
                        endTime2 = int(endTimeAll[colonval+1:])
                        signalData.signalTiming[strCso].endTime = dta.Time(endTime1, endTime2)
                    elif ":" in cellValue and len(cellValue)==5:
                        if gotStartTime2 == False:
                            startTime1 = int(cellValue[:2])
                            startTime2 = int(cellValue[3:])
                            signalData.signalTiming[strCso].startTime = dta.Time(startTime1, startTime2)
                            gotStartTime2 = True
                        else:
                            endTime1 = int(cellValue[:2])
                            endTime2 = int(cellValue[3:])
                            signalData.signalTiming[strCso].endTime = dta.Time(endTime1, endTime2)
                    elif ":" in cellValue and "TO" in cellValue:
                        stopval = cellValue.find("TO")
                        startTimeAll = cellValue[:stopval-1]
                        colonval = startTimeAll.find(":")
                        startTime1 = int(startTimeAll[:colonval])
                        startTime2 = int(startTimeAll[colonval+1:])
                        signalData.signalTiming[strCso].startTime = dta.Time(startTime1, startTime2)
                        endTimeAll = cellValue[stopval+3:]
                        colonval = endTimeAll.find(":")
                        endTime1 = int(endTimeAll[:colonval])
                        endTime2 = int(endTimeAll[colonval+1:])
                        signalData.signalTiming[strCso].endTime = dta.Time(endTime1, endTime2)
                    elif ":" in cellValue and "to" in cellValue:
                        stopval = cellValue.find("to")
                        startTimeAll = cellValue[:stopval-1]
                        colonval = startTimeAll.find(":")
                        startTime1 = int(startTimeAll[:colonval])
                        startTime2 = int(startTimeAll[colonval+1:])
                        signalData.signalTiming[strCso].startTime = dta.Time(startTime1, startTime2)
                        endTimeAll = cellValue[stopval+3:]
                        colonval = endTimeAll.find(":")
                        endTime1 = int(endTimeAll[:colonval])
                        endTime2 = int(endTimeAll[colonval+1:])
                        signalData.signalTiming[strCso].endTime = dta.Time(endTime1, endTime2)

        #dta.DtaLogger.info("Operating times are from %s to %s for CSO %s" % (signalData.signalTiming[strCso].startTime, signalData.signalTiming[strCso].endTime, strCso))
        
    found = False
    # search down rows from top left row for CYCLE keyword
    for x in range(i-5, i + 7):
        # search over starting at phase data column for CYCLE keyword
        for y in range(j, j + 13):
            keyword = str(sheet.cell_value(x, y)).strip().upper()
            if keyword == "CYCLE":  #find the row with the CYCLE keyword
                found = True
                colCycle = y
                streetCell = findStreet(sheet,signalData.topLeftCell)
                z = streetCell[0]
                for k in range(x + 1, z): #search down until just before signal phasing section
                    cso = []
                    for l in range(y, y + 6): # search six cells to the right of CYCLE column 
                        cellValue = sheet.cell_value(k, l)
                        if str(cellValue).strip() == "":
                            continue
                        if cellValue:
                            if isinstance(cellValue, float):
                                cellValue = str(int(cellValue))
                            elif isinstance(cellValue, int):
                                cellValue = str(cellValue)
                            cso.append(cellValue)
                    csosub = cso[:1]
                    strCso = "".join(cso)
                    strCsoSub = "".join(csosub)

                    if not strCso:
                        continue
                    #dta.DtaLogger.debug("I found CSO: %s" % strCso)
                    CSOMatch = False
                    # Syntax was changed so that only the first digit of the CSO is matched now instead of the whole number
                    for signalTimes in signalData.signalTiming:
                        if signalTimes[:1]==strCsoSub:
                            CSOMatch = True
                            strCso = signalTimes
                    if not CSOMatch:
                        # Corrects for CSOs that are FREE or --- by assigning them to CSO of 111 which is All Other Times
                        # or to any CSO that exists in the phasing if there is no 111
                        if strCsoSub[0]=="F" or strCsoSub[0]=="-":
                            for signalTimes in signalData.signalTiming:
                                if signalTimes[:1]=="1":
                                    CSOMatch = True
                                    strCso = signalTimes
                                else:
                                    CSOMatch = True
                                    strCso = signalTimes
                        else:
                            timename = str(sheet.cell_value(k,y-11))
                            timename = timename.strip()
                            timenamesub = timename[:1]
                            if timenamesub == "P":
                                continue
                    if not CSOMatch:
                        dta.DtaLogger.error("ERROR CSO %s does not exist in the timing section. Please manually correct the signal" % strCso)
                        continue
                    setTimes(k,colCycle,sheet)  #set the start and end times
    if found == False:
        raise ParsingCardError("I cannot find start and end times") 

def getSignalIntervalData(sheet, signalData):
    """
    Assigns the signal interval data from the supplied excel
    spreadsheet 
    """
    if not signalData.sigInterCell or not signalData.colPhaseData:
        return
    #begin with the signal interval data
    # Commented-out lines are from a previous version of the code.  The current version searches a set number of rows, whereas the other one would break once an empty line was hit.
    # This meant that not all CSOs were read in, while having it read down 11 rows gets all of the CSOs for all signals.
    startX, startY = signalData.sigInterCell
    #find the first enry
    #i = startX
    finishedReadingData = False
    #while True:
    #    i += 1
    for i in range (startX+1,startX+11):
        try:
            value = sheet.cell_value(rowx=i, colx=startY)
        except IndexError:
            break
        
        if str(value).strip():
            finishedReadingData = True
            if "NOTE" in str(value).strip() or "*" in str(value).strip():
                break
            row = []
            for j in range(startY, signalData.colPhaseData):
                value = sheet.cell_value(rowx=i,colx=j)

                if str(value):
                    row.append(value)
                    
            #you ought to have 3 values: CSO, CYCLE, OFFSET
            #read the rest of the row
            signalTiming = ExcelSignalTiming()

            try:
                strCso = str(int(float(row[0])))
            except ValueError, e:
                strCso = str(row[0])

            if len(row) == 3:
                strCycle = str(row[1])
                strOffset = str(row[2])
            elif len(row) == 2:
                strCycle = str(row[1])
                strOffset = "-"
            elif len(row) == 1:
                strCycle = "-"
                strOffset = "-"
            else:
                raise ParsingCardError("I cannot parse the cso,cycle,offset from row %d:%s"
                                        % (i, str(row)))              
            signalTiming.cso = strCso
            if strCso.endswith("-") and len(strCso) == 3 and strCycle != "--":
                signalTiming.offset = 0
                if strCso.endswith("--"):
                    signalTiming.cycle = 0
                    signalTiming.isActuated = True
                else:
                    signalTiming.cycle = float(strCycle)
            elif strCso.endswith("--") and len(strCso) == 4 and strCycle == "--":
                signalTiming.offset = 0
                signalTiming.cycle = float(strCycle)
            elif strCso == "FREE" or strCso == "free":
                signalTiming.isActuated = True
            else:
                try:
                    if "-" in strCycle:
                        signalTiming.cycle = None
                        signalTiming.isActuated = True
                    else:
                        signalTiming.cycle = float(strCycle)    
                    signalTiming.offset = float(strOffset) if "-" not in strOffset  else 0
                except ValueError, e:
                    raise ParsingCardError("I could not parse the cso,cycle,offset from row %d:%s"
                                           ", Error: %s" % (i, str(row), str(e)))
                
            j = signalData.colPhaseData
            timings = []
            signalData.signalTiming[signalTiming.cso] = signalTiming
            while True:
                try:
                    value = sheet.cell_value(rowx=i,colx=j)
                except IndexError:
                    if not signalData.lastColPhaseData:
                        signalData.lastColPhaseData = j - 1
                    signalData.sIntervals[row[0]] = row
                    signalTiming.setPhaseTimes(timings)
                    break                    
                if str(value).strip(): # I chanched this from value to value.strip()
                    if not isinstance(value, (int, float)):
                        raise ParsingCardError("The signal intervals are not numbers "
                                               "in row with CSO= %s. Problem reading: %s at %d, %d " 
                                               % (strCso, str(value), i, j))
                                           
                    row.append(value)
                    timings.append(value)
                #if you find an empty cell: stop reading 
                # and set the lastColPhaseData attribute 
                else:
                    if not signalData.lastColPhaseData:
                        signalData.lastColPhaseData = j - 1
                    signalData.sIntervals[row[0]] = row
                    signalTiming.setPhaseTimes(timings)
                    break
                j += 1
        else:
            continue
def fillPhaseInfo(phaseInfo):
    """The a list with the phase info for a movement group such as
       ['', u'G', '', u'Y', u'R', '', '', '', '', '', '', '']
       fill the empty strings with the state of the signal
    """
    for i in range(1, len(phaseInfo)):
        if phaseInfo[i] == "" and phaseInfo[i - 1] != "":
            phaseInfo[i] = phaseInfo[i - 1]
            
    for i in range(len(phaseInfo) - 1, -1, -1):
        if phaseInfo[i] == "" and phaseInfo[i + 1] != "":
            phaseInfo[i] = phaseInfo[i + 1]
                
def getPhasingData(sheet, signalData):

    if not signalData.phaseSeqCell:
        raise ParsingCardError("I cannot parse its phasing data1.")
    
    if not signalData.sigInterCell:
        raise ParsingCardError("I cannot parse its phasing data2.")

    if not signalData.colPhaseData:
        raise ParsingCardError("I cannot parse its phasing data3.")

    if not signalData.lastColPhaseData:
        raise ParsingCardError("I cannot parse its phasing data4.")        

    startX, startY = signalData.phaseSeqCell 

    endX, endY = signalData.sigInterCell

    movementIndex = 1
    phasingData = []
    movementNames = []

    intervalStateGreen = ["G", "G+G", "G*", "G G", "FY", "F", "G+F", "U", "T", "G + G", "G + F", "ON"]
    intervalStateYellow = ["Y", "SY"]
    intervalStateRed = ["R", "RH", "OFF", "FR"]
    intervalStateBlank = [""]
    intervalStateValid = intervalStateGreen + intervalStateYellow + \
        intervalStateRed + intervalStateBlank

    for i in range(startX + 1, endX):
        groupMovement = str(sheet.cell_value(rowx=i, colx=startY)).upper()
                               
        if groupMovement == "" or "PEDS " in groupMovement or "PED " in groupMovement \
                or "XING " in groupMovement or "MUNI " in groupMovement or \
                "TRAIN " in groupMovement or "FLASHING " in groupMovement or \
                groupMovement == "MUNI" or " MUNI" in groupMovement or "RAIL" in groupMovement:
            continue
        else:
            #read the row of phasing states 
            singleMovementData = []
            allIntervalStatesValid = True
            for j in range(signalData.colPhaseData, signalData.lastColPhaseData + 1):
                intervalState = str(sheet.cell_value(rowx=i, colx=j)).upper().strip()
                if intervalState == "":
                    singleMovementData.append("")
                elif intervalState in intervalStateGreen:
                    singleMovementData.append("G")
                elif intervalState in intervalStateYellow:
                    singleMovementData.append("Y")
                elif intervalState in intervalStateRed or "-R-" in intervalStateRed:
                    singleMovementData.append("R")
                else:
                    allIntervalStatesValid = False
                    break
            if not allIntervalStatesValid:
                continue
            if singleMovementData == ["",] * len(singleMovementData):
                continue

        setattr(signalData, "gMov%d" % movementIndex, groupMovement)
        movementIndex += 1
        movementNames.append(groupMovement)

        phasingData.append(singleMovementData)        
    
    if phasingData == []:
        raise ParsingCardError("I cannot parse its phasing data")
    numIntervals = len(phasingData[0])
    if numIntervals == 0:
        raise ParsingCardError("I cannot parse its phasing data. "
                               "The number of phase intervals is zero")
    
    if len(phasingData) <= 1:
        raise ParsingCardError("Signal has less than two group movements")

    if len(phasingData) > 1:
        for i in range(len(phasingData) - 1):
            if len(phasingData[i]) != len(phasingData[i+1]):
                raise ParsingCardError("I cannot parse its phasing data. "
                               "Different number of phasing steps for "
                                       "different phasing movements")


    if signalData.getNumTimeIntervals() != len(phasingData[0]):
        raise ParsingCardError("The number of phase states %d is not the same "
                               "with the number of its signal intervals %d" % 
                               (len(phasingData[0]), 
                                signalData.getNumTimeIntervals()))

    ma = MultiArray("S1", [movementNames, range(1, numIntervals + 1)])
    for i, movName in enumerate(ma.getElementsOfDimention(0)):
        fillPhaseInfo(phasingData[i])
        for j in range(1, numIntervals + 1):
            intervalState = str(phasingData[i][j-1]).strip().upper()
            if intervalState == "G":
                ma[movName, j] = "G" # GREEN
            elif intervalState == "Y":
                ma[movName, j] = "Y" # YELLOW
            elif intervalState == "R":
                ma[movName, j] = "R" #RED
            else:
                raise ParsingCardError("Group movement \t%s\t. I cannot interpret "
                                       "the phase status \t%s" % (movName, intervalState))
                
    signalData.phasingData = ma

def extractStreetNames(intersection):
    """Split the Excel intersection string to two or more streetNames"""

    intersection = intersection.upper()
    regex = re.compile(r",| AND|\&|\@|\ AT|\/")
    streetNames = regex.split(intersection)
    if len(streetNames) == 1:
        #log the error
        pass
    result = sorted([name.strip() for name in streetNames])
    return result

def cleanStreetName(streetName):

    corrections = {"TWELFTH":"12TH", 
                   "ELEVENTH":"11TH",
                   "TENTH":"10TH",
                   "NINTH":"9TH",
                   "EIGHTH":"8TH",
                   "SEVENTH":"7TH",
                   "SIXTH":"6TH",
                   "FIFTH":"5TH",
                   "FOURTH":"4TH",
                   "THIRD":"3RD",
                   "SECOND":"2ND",
                   "FIRST":"1ST",
                   "O'FARRELL":"O FARRELL",
                   "3RDREET":"3RD",
                   "EMBARCADERO/KING":"THE EMBARCADERO",
                   "VAN NESSNUE":"VAN NESS",
                   "3RD #3":"3RD",
                   "BAYSHORE #3":"BAYSHORE",
                   "09TH":"9TH",
                   "08TH":"8TH",
                   "07TH":"7TH",
                   "06TH":"6TH",
                   "05TH":"5TH",
                   "04TH":"4TH",
                   "03RD":"3RD",
                   "02ND":"2ND",
                   "01ST":"1ST"}


    itemsToRemove = [" STREETS",
                     " STREET",
                     " STS.",
                     " STS",
                     " ST.",
                     " ST",
                     " ROAD",
                     " RD.",
                     " RD",
                     " AVENUE",
                     " AVE.",
                     " AVES",
                     " AVE",
                     " BLVD.",
                     " BLVD",
                     " BOULEVARD",
                     "MASTER:",
                     " DRIVE",
                     " DR.",
                     " WAY",
                     " WY",
                     " CT",
                     " TERR",
                     " HWY"]

    newStreetName = streetName.strip()
    for wrongName, rightName in corrections.items():
        if wrongName in streetName:
            newStreetName = streetName.replace(wrongName, rightName)
        if streetName == 'EMBARCADERO':
            newStreetName = "THE EMBARCADERO"
        if streetName.endswith(" DR"):
            newStreetName = streetName[:-3]
        if streetName.endswith(" AV"):
            newStreetName = streetName[:-3]
        if " TO " in streetName:
            cutOff = streetName.find(" TO ")
            newStreetName = streetName[:cutOff]
            

    for item in itemsToRemove:
        if item in newStreetName:
            newStreetName = newStreetName.replace(item, "")

    return newStreetName.strip()

    

def cleanStreetNames(streetNames):
    """Accept street names as a list and return a list 
    with the cleaned street names"""
    
    newStreetNames = map(cleanStreetName, streetNames)
    if len(newStreetNames) > 1 and newStreetNames[0] == "":
        newStreetNames.pop(0)
    return newStreetNames

def parseExcelCardFile(directory, fileName):
    """
    Reads the excel file, parses its information and returns
    as a :py:class:`SignalData` object.
    """
    sd = SignalData()
    sd.fileName = fileName
    book = xlrd.open_workbook(os.path.join(directory, fileName))
    sheet = book.sheet_by_index(0)
        
    sd.topLeftCell = findTopLeftCell(sheet)
    sd.iName = getIntersectionName(sheet, sd.topLeftCell)
    try:
        sd.phaseSeqCell = findStreet(sheet, sd.topLeftCell)
    except ParsingCardError, e:
        msg = 'Unable to find the start of the phasing section. filename %s' % fileName
        raise ParsingCardError(msg)
    sd.pedPhaseCell = findPedestrianPhase(sheet, sd.topLeftCell)
    sd.sigInterCell = findSignalIntervals(sheet, sd.topLeftCell)    
    getFirstColumnOfPhasingData(sheet, sd)
    getSignalIntervalData(sheet, sd)

    getOperationTimes(sheet, sd)

    getPhasingData(sheet, sd)
    return sd


def parseExcelCardsToSignalObjects(directory,fileName):
    """
    Reads the raw excel cards, extracts all the relevant information, instantiates 
    for each excel file an object called SignalData and stores the information
    and then pickles all the signal data objects into a file named: 

    excelCards.pkl
    """
    if not fileName.endswith("xls"):
        return False
    if fileName.startswith("System"):
        return False
    try:
        sd = parseExcelCardFile(directory, fileName)

    except ParsingCardError, e:
        dta.DtaLogger.error("Error parsing %-40s: %s" % (fileName, str(e)))
        problemCards.append(sd)
        sd.error = e
        return False
    else:
        excelCards = sd

    return excelCards

def mapStreetNamesForManuallyMappedNodes(network, cards):
    """
    This function maps street names for the nodes that have been manually mapped    
    """
    CUTOFF = 0.7 # this parameter controls how close the strings need be
    result = []
    for card in cards:        
        streetNames = card.streetNames        
        node = net.getNodeForId(card.mappedNodeId)

        if len(node.getStreetNames(incoming=True, outgoing=False)) != len(streetNames):
            print card.fileName, "\t", card.mappedNodeId, "\t", node.getStreetNames(), "\t", streetNames
            continue
            
        baseStreetNames = node.getStreetNames(incoming=True, outgoing=False)
        for bName, mName in izip(baseStreetNames, streetNames):
            if not difflib.get_close_matches(bName, [mName], 1, CUTOFF):
                print card.fileName, "\t", card.mappedNodeId, "\t", node.getStreetNames(), "\t", streetNames, "\t", bName, "\t", mName
                break
            card.mappedStreet[mName] = bName            
        else:
            card.mappedNodeName = node.getName()
            result.append(card)
    return result

def findNodeWithSameStreetNames(network, excelCard, CUTOFF, mappedNodes):
    """
    Given a :py:class:`dta.DynameqNetwork` instance, *network*, and 
    an instance of ?, *excelCard*,
    looks for the :py:class:`dta.RoadNode` instance in the *network* with matching
    streetnames to the excelCard.streetNames
    
    Returns True if a match is found, and sets:
      * excelCard.mappedStreet
      * excelCard.mappedNodeName
      * excelCard.mappedNodeId
      
    """

    streetNames = excelCard.streetNames
    dta.DtaLogger.debug("Street names for mapping are %s" % streetNames)     

    for node in network.iterRoadNodes():
        if node.getId() in mappedNodes.values():
            continue
        
        baseStreetNames = node.getStreetNames(incoming=True, outgoing=False)
        baseStreetNames_cleaned = [cleanStreetName(bs) for bs in baseStreetNames]
        baseStreetNames_cleaned = set(baseStreetNames_cleaned)
        baseStreetNames_cleaned = sorted(baseStreetNames_cleaned)

        if len(baseStreetNames_cleaned) != len(streetNames):
            #dta.DtaLogger.error("Street names different lengths")
            continue
        
        for idx in range(len(baseStreetNames_cleaned)):
            if not difflib.get_close_matches(baseStreetNames_cleaned[idx], [streetNames[idx]], 1, CUTOFF):
                break

            ## The node selection for this intersection has to be hard-coded since there are two intersections between the same streets and one does
            ## not have the turns that are included in the signal phases
            elif streetNames == ['19TH', 'WINSTON'] and node.getId()!= 23069:
                break
            elif streetNames == ['23RD', 'POTRERO'] and node.getId()!= 23962:
                break
            elif streetNames == ['25TH', 'POTRERO'] and node.getId()!= 23952:
                break
            elif streetNames == ['DONAHUE', 'INNES'] and node.getId()!= 51690:
                break
            else:
                excelCard.mappedStreet[streetNames[idx]] = baseStreetNames_cleaned[idx]
       
        else:
            mappedNodes[excelCard.iiName] = node.getId()
            excelCard.mappedNodeName = node.getName()
            excelCard.mappedNodeId = node.getId()

            return True
    return False

def groupMovementToTurnType(gMovName, addRightToThru=True):
    """
    Given *gMovName*, which is the string from the Excel file representing a movement (e.g. ``OTIS SBLT`` or ``17TH ST. W/B THRU``),
    returns a list of relevant turntypes corresponding to the :py:meth:`Movement.getTurnType` method.
    
    Pass *addRightToThru* if you want the through movements to come with right turns automatically.

    the function is being called by mapGroupMovments
    """

    result = []

    #todo ends with LT
    leftTurnIndicators = ["LT'S","-L", "WB-L","EB-L","SB-L","NB-L","LEFT TURN", " LT", "NBLT", "SBLT", "WBLT", "EBLT","(NBLT)", "(SBLT)", "(WBLT)", "(EBLT)"]
    rightTurnIndicators = ["RIGHT TURN", "BRT", " RT"]
    thruTurnIndicators = [" THRU", " THROUGH", "(THRU)"]

    indicators = {TURN_LEFT:leftTurnIndicators, TURN_THRU:thruTurnIndicators, TURN_RIGHT:rightTurnIndicators}
    result = []
    thruadded=0
    movadded=0
    for dir, dirIndicators in indicators.items():
        for indicator in dirIndicators:
            if indicator in gMovName:
                result.extend(dir)
                movadded = 1
                if dir==TURN_THRU:
                    thruadded=1
            if dir==TURN_RIGHT and thruadded==1 and addRightToThru:
                result.extend(dir)
    return result

    
def mapMovements(mec, baseNetwork):
    
    #dta.DtaLogger.info("Number of excel cards read %d" % len(excelCards))
    
    def getStreetName(gMovName, streetNames):
        """Finds to which street the movement applies to and returns the 
        street"""
        for i in range(len(streetNames)):
            if "3" in gMovName and not "23" in gMovName:
                if "3" in streetNames[i] and not "23" in streetNames[i]:
                    return streetNames[i]
            elif "23" in gMovName:
                if "23" in streetNames[i]:
                    return streetNames[i]
            elif "BROADWAY" in gMovName:
                if "TUNNEL" in gMovName:
                    if "BROADWAY" in streetNames[i] and "TUNNEL" in streetNames[i]:
                        return streetNames[i]
                elif "TUNNEL" not in gMovName:
                    if "BROADWAY" in streetNames[i] and "TUNNEL" not in streetNames[i]:
                        return streetNames[i]
            else:
                if streetNames[i] in gMovName:
                    return streetNames[i]

        matches = difflib.get_close_matches(gMovName, streetNames, 1)
        if matches:
            bestMatchedStreetName = matches[0]
            return bestMatchedStreetName
        else:
            raise StreetNameMappingError("The group movement is not associated with any of the "
                                         "street names that identify the intersection.#%s#%s" % 
                                         (gMovName, str(streetNames)))
            
    def getDirections(gMovName):
        """Searches the movement name for a known set of strings that indicate the
        direction of the movment and returns the result(=the direction of the movement)
        Returns a string representing the direction: WB, EB, NB, SB
        """
        wbIndicators = ["WB-","WB,", ",WB", "WB/", "/WB", "WB&", "&WB", " WB", "WB ", "W/B", "(WB", "WB)", "(WB)", "(WESTBOUND)", "WESTBOUND", "WEST "]
        ebIndicators = ["EB-","EB,", ",EB", "EB/", "/EB","EB&", "&EB", " EB", "EB ", "E/B", "(EB", "(EB)", "EB)", "(EASTBOUND)", "EASTBOUND", "EAST "]        
        nbIndicators = ["NB-","NB,", ",NB", "NB/", "/NB","NB&", "&NB", " NB", "NB ", "N/B", "(NB", "NB)", "(NORTHBOUND)", "NORTHBOUND", "NORTH "]
        sbIndicators = ["SB-","SB,", ",SB", "SB/", "/SB","SB&", "&SB", " SB", "SB ", "S/B", "(SB", "SB)", "(SOUTHBOUND)", "SOUTHBOUND", "SOUTH "]
        
        indicators = {"WB":wbIndicators, "EB":ebIndicators, "NB":nbIndicators, "SB":sbIndicators}
        
        result = []
        for dir, dirIndicators in indicators.items():
            for indicator in dirIndicators:
                if indicator in gMovName and gMovName != "SOUTH VAN NESS" and gMovName != "WEST PORTAL" and gMovName != "NORTH POINT" and gMovName != "I-80 E OFF-RAMP" and gMovName != "HWY 101 SOUTHBOUND RAMP":
                   result.append(dir)
                   break
       
        return result


    def mapGroupMovements(mec, groupMovementNames, bNode):
        """
        Output: populate the mappedMovements dictonary of the excelCard object.
        The keys of the dictonary are the movement names and its values are
        all the iids of the corresponding movemens
        """

        streetNames = list(mec.streetNames)
        for gMovName in groupMovementNames:
            if "DRIVEWAY" in gMovName or "FIRE HOUSE" in gMovName or ("BRIDGE " in gMovName and "CAMBRIDGE" not in gMovName) or "RESTRICTION" in gMovName or \
               "PIER 39" in gMovName or " PEDS" in gMovName or "SERVICE ROAD" in gMovName or ("PARKING" in gMovName and "CHURCH" not in gMovName) or \
               "GARAGE" in gMovName or "(EMS" in gMovName or "LRV PREEMPT" in gMovName or "AT BRIDGE" in gMovName or "BLIND " in gMovName or "STREETCAR" in gMovName or \
               "(FAR" in gMovName or "SHRADER PATH" in gMovName or " WBRT" in gMovName or " RT. TURN" in gMovName or "XING" in gMovName or "PEDS " in gMovName:
                continue


            #for each group movement get the approach's street name
            try:
                stName = getStreetName(gMovName, streetNames)
            except StreetNameMappingError, e:
                raise StreetNameMappingError("%s#%d#%s" % (mec.fileName, mec.mappedNodeId, str(e)))
            gTurnTypes = groupMovementToTurnType(gMovName)
            gDirections = getDirections(gMovName)
            if "BUSH" in gMovName and "WB" in gDirections:
                continue
            if "FELL" in gMovName and "EB" in gDirections and "WB" not in gDirections and "FRANKLIN" not in streetNames:
                continue
            if "PINE" in gMovName and "EB" in gDirections:
                continue
            if "PIERCE" in gMovName and "FELL" in streetNames and "NB" in gDirections:
                continue
            if "LEE" in gMovName and "OCEAN" in streetNames and "SB" in gDirections:
                continue

            f = open("temp_directons.txt", "a")
            f.write("%25s%20s%20s\n" % (gMovName, str(gTurnTypes), str(gDirections)))
            f.close()

            if not stName:
                dta.DtaLogger.error("I cannot identify the approach of the group "
                                                 "movement %s in node %s stored as %s" 
                                                 % (gMovName, mec.iName, mec.iiName))
                raise MovementMappingError("I cannot identify the approach of the group "
                                                 "movement %s in node %s stored as %s" 
                                                 % (gMovName, mec.iName, mec.iiName))
            bStName = mec.mappedStreet[stName]
            #collect all the links of the approach that have the same direction
            gLinks = []
            if "3" in bStName and "23" not in bStName:
                candLinks = [link for link in bNode.iterIncomingLinks() if "3" in link.getLabel() and "23" not in link.getLabel()]
            elif "3" in bStName and "23" in bStName:
                candLinks = [link for link in bNode.iterIncomingLinks() if "3" in link.getLabel() and "23" in link.getLabel()]
            elif "BROADWAY" in bStName and "TUNNEL" not in bStName:
                candLinks = [link for link in bNode.iterIncomingLinks() if "BROADWAY" in link.getLabel() and "TUNNEL" not in link.getLabel()]
            elif "BROADWAY" in bStName and "TUNNEL" in bStName:
                candLinks = [link for link in bNode.iterIncomingLinks() if "BROADWAY" in link.getLabel() and "TUNNEL" in link.getLabel()]
            else:
                candLinks = [link for link in bNode.iterIncomingLinks() if bStName in link.getLabel()]
            for candLink in candLinks:
                if gDirections:
                    if set(getPossibleLinkDirections(candLink)) & set(gDirections):
                        gLinks.append(candLink)
                else:
                    gLinks.append(candLink)

            #dta.DtaLogger.info("Movement %s for street %s has links %s" % (gMovName,bStName,str([links.getLabel() for links in gLinks])))

            if len(gLinks) == 0:
                dta.DtaLogger.error("%s#%d#I cannot identify the links for the group "
                               "movement #%s# in node #%s# stored as #%s# candidate links are # %s" %
                                                 (mec.fileName, mec.mappedNodeId, gMovName, mec.iName, mec.iiName, str(["%s,%s" % (candLink.getLabel(), candLink.getDirection()) for candLink in candLinks])))

                raise MovementMappingError("%s#%d#I cannot identify the links for the group "
                               "movement #%s# in node #%s# stored as #%s# candidate links are # %s" %
                                                 (mec.fileName, mec.mappedNodeId, gMovName, mec.iName, mec.iiName, str(["%s,%s" % (candLink.getLabel(), candLink.getDirection()) for candLink in candLinks])))

            gMovements = []
            if gTurnTypes:
                availableMovs = list(chain(*[link.iterOutgoingMovements() for link in gLinks]))
                for mov in availableMovs:
                    if mov.getTurnType() in gTurnTypes:
                        gMovements.append(mov)

                if len(gMovements) == 0:
                    dta.DtaLogger.error("%s # %d # cannot identify the movements for the group "
                  "movement %s in node %s stored as %s. The streetNames are: %s , the directions of the group are %s. The available movemements are %s" 
                                           % (mec.fileName, mec.mappedNodeId, gMovName, mec.iName, mec.iiName,
                                              str(mec.streetNames), str(gDirections), str([mov.getDirection() for mov in availableMovs])))
                    raise MovementMappingError("%s # %d # cannot identify the movements for the group "
                  "movement %s in node %s stored as %s. The streetNames are: %s , the directions of the group are %s. The available movemements are %s" 
                                           % (mec.fileName, mec.mappedNodeId, gMovName, mec.iName, mec.iiName,
                                              str(mec.streetNames), str(gDirections), str([mov.getDirection() for mov in availableMovs])))
                       
            else:    
                gMovements = list(chain(*[link.iterOutgoingMovements() for link in gLinks]))

            if len(gMovements) == 0:
                dta.DtaLogger.error("%s#%d#I cannot identify the movements for the group "
                  "movement %s in node %s stored as %s. The streetNames are: %s , the directions of the group are %s " 
                                           % (mec.fileName, mec.mappedNodeId, gMovName, mec.iName, mec.iiName, 
                                              str(mec.streetNames), str(gDirections)))
                raise MovementMappingError("%s#%d#I cannot identify the movements for the group "
                  "movement %s in node %s stored as %s. The streetNames are: %s , the directions of the group are %s " 
                                           % (mec.fileName, mec.mappedNodeId, gMovName, mec.iName, mec.iiName, 
                                              str(mec.streetNames), str(gDirections)))
##                raise MovementMappingError("%s#%d#I cannot identify the movements for the group "
##                  "movement %s in node %s stored as %s. The streetNames are: %s , the directions of the group are %s " 
##                                           % (mec.fileName, mec.mappedNodeId, gMovName, mec.iName, mec.iiName, 
##                                              str(mec.streetNames), str(gDirections)))
#                    `                       "group are: %s and the turn types: %s" %     
#                                                 (gMovName, mec.iName, mec.iiName, gDirections, gTurnTypes))
                

            gMovements = sorted(gMovements, key = lambda elem: elem.getId())

            for mov in gMovements:
                mec.mappedMovements[gMovName].append(mov.getId())

    index = defaultdict(int)
    ## Commented lines are from format change.  Original code parsed all of the signal cards, then mapped them, then created time phases.
    ## New code performs all processes on one excel card before moving to the next card.
    
    #excelCardsWithMovements = []    
    #for mec in excelCards:

    if mec.mappedNodeId == -9999:
        return False
    #if there is a match
    if mec.mappedNodeId and mec.phasingData: 
        #get the mapped node
        if not baseNetwork.hasNodeForId(int(mec.mappedNodeId)):
            return False
        bNode = baseNetwork.getNodeForId(int(mec.mappedNodeId))
        #get the groupMovementNames
        groupMovementNames = mec.phasingData.getElementsOfDimention(0)

        numGroupMovements = len(groupMovementNames)
        numSteps = len(mec.phasingData.getElementsOfDimention(1))
        index[numGroupMovements] += 1

        #for each group movement get the approach's street name
        
        try:
            mapGroupMovements(mec, groupMovementNames, bNode)
        except MovementMappingError, e:
            dta.DtaLogger.error("MovementMappingError:   %s" % str(e))
            return False
        except StreetNameMappingError, e:
            dta.DtaLogger.error("StreetNameMappingError: %s" % str(e))
            return False

    if len(mec.mappedMovements) == 0:
        dta.DtaLogger.error("Signal %s. No mapped movements" % mec.fileName)
        return False
        #raise MovementMappingError("Signal %s. No mapped movements" % mec.fileName)

    if len(mec.mappedMovements) == 1:
        dta.DtaLogger.error("Signal %s. Only one of the group movements has been mapped" %
                        mec.fileName)
        return False
#       raise MovementMappingError("Signal %s. Cannot generate a signal with "
#                               "only one movement" % mec.fileName)

    groupMovements = mec.phasingData.getElementsOfDimention(0)
    streetNames = list(mec.streetNames)
    for gMovName in groupMovements:
        if "DRIVEWAY" in gMovName or "FIRE HOUSE" in gMovName or ("BRIDGE " in gMovName and "CAMBRIDGE" not in gMovName) or "RESTRICTION" in gMovName or \
           "PIER 39" in gMovName or " PEDS" in gMovName or "SERVICE ROAD" in gMovName or ("PARKING" in gMovName and "CHURCH" not in gMovName) or \
           "GARAGE" in gMovName or "(EMS" in gMovName or "LRV PREEMPT" in gMovName or "AT BRIDGE" in gMovName or "BLIND " in gMovName or "STREETCAR" in gMovName or \
           "(FAR" in gMovName or "SHRADER PATH" in gMovName or " WBRT" in gMovName or " RT. TURN" in gMovName or "XING" in gMovName or "PEDS " in gMovName:
            MovementNames = list(groupMovements)
            MovementNames.remove(gMovName)
            groupMovements = tuple(MovementNames)

        gTurnTypes = groupMovementToTurnType(gMovName)
        gDirections = getDirections(gMovName)
        if "BUSH" in gMovName and "WB" in gDirections:
            MovementNames = list(groupMovements)
            MovementNames.remove(gMovName)
            groupMovements = tuple(MovementNames)
        if "FELL" in gMovName and "EB" in gDirections and "WB" not in gDirections and "FRANKLIN" not in streetNames:
            MovementNames = list(groupMovements)
            MovementNames.remove(gMovName)
            groupMovements = tuple(MovementNames)
        if "PINE" in gMovName and "EB" in gDirections:
            MovementNames = list(groupMovements)
            MovementNames.remove(gMovName)
            groupMovements = tuple(MovementNames)
        if "PIERCE" in gMovName and "FELL" in streetNames and "NB" in gDirections:
            MovementNames = list(groupMovements)
            MovementNames.remove(gMovName)
            groupMovements = tuple(MovementNames)
        if "LEE" in gMovName and "OCEAN" in streetNames and "SB" in gDirections:
            MovementNames = list(groupMovements)
            MovementNames.remove(gMovName)
            groupMovements = tuple(MovementNames)  
        
    if len(mec.mappedMovements) != len(groupMovements):
        dta.DtaLogger.error("Signal %s. Not all movements have been mapped" % mec.fileName)
        return False

#       raise MovementMappingError("Signal %s. Not all movements have been mapped" %
#                               mec.fileName)
    for gMov in groupMovements:
        if len(mec.mappedMovements[gMov]) == 0:
            dta.DtaLogger.error("Signal %s. The group movement %s is not mapped to "
                            "any link movements" % (mec.fileName, gMov))
            return False
#           raise MovementMappingError("Signal %s. The group movmement %s is not mapped"
#                                   "to any link movemenets" % (mec.iName, gMov))
    excelCardsWithMovements = (mec)

    return excelCardsWithMovements

def checkNumberofTimes(excelCard, startTime, endTime):
#   checks to see if a card has more than one CSO that matches the start and end time 
    nummatches = 0
    for signalTiming in excelCard.iterSignalTiming():
        if startTime >= signalTiming.startTime and endTime <= signalTiming.endTime and signalTiming.endTime>signalTiming.startTime:
                nummatches += 1
    if nummatches >1:
        for signalTiming in excelCard.iterSignalTiming():
            dta.DtaLogger.error("Timing is start time %s, end time %s for CSO %s" % (signalTiming.startTime, signalTiming.endTime, signalTiming.cso)) 
    return nummatches
  
def selectCSO(excelCard, startTime, endTime):
    """
    returns the ExcelSignalTiming if there is one that is in operation during the 
    input hours. Otherwise it returns none
    """
    ## Changed from looping through all cards here to looping through the cards in the main program section
##    for signalTiming in excelCard.iterSignalTiming():
##        dta.DtaLogger.error("start time is %s, end time is %s" % (signalTiming.startTime,signalTiming.endTime))
    
    for signalTiming in excelCard.iterSignalTiming():
        if startTime >= signalTiming.startTime and startTime <= signalTiming.endTime and signalTiming.endTime>signalTiming.startTime:
            return signalTiming
    for signalTiming in excelCard.iterSignalTiming():
        if signalTiming.startTime == dta.Time(0,0) and signalTiming.endTime == dta.Time(0,0):
            return signalTiming
    for signalTiming in excelCard.iterSignalTiming():
        if signalTiming.startTime == dta.Time(0,0) and signalTiming.endTime == dta.Time(23,59):
            return signalTiming
    return None

    ## Assocated with pickle testing not used   
##def readNetIndex():
##
##    index = {}
##    for line in open('/Users/michalis/Documents/myPythonPrograms/pbTools/pbProjects/doyleDTA/indexSep1v2.txt'):
##        links = line.strip().split(',')
##        index[tuple(links[0].split())] = [tuple(link.split()) for link in links[1:]]
##    
##    return index

##def pickleCards(outfileName, cards):
##    """
##    Pickle the cards into the outfileName
##    """
##    outputStream = open(outfileName, "wb")
##    pickle.dump(cards, outputStream)
##    outputStream.close()    
##
##def unPickleCards(fileName):
##    """
##    Unpickle the cards stored in the file and return them
##    """
##    pkl_file = open(fileName, "rb")
##    excelCards = pickle.load(pkl_file)
##    pkl_file.close()
##    return excelCards

def getExcelFileNames(directory):

    fileNames = []
    for fileName in os.listdir(directory):
        if not fileName.endswith("xls"):
            continue
        if fileName.startswith("System"):
            continue
        fileNames.append(fileName)

    return fileNames

##def mapIntersections(excelCards, mappedingFile):
##    """for each excel card in the find the node in the base network 
##    it corresponds"""
##
##    mapping = {}
##    cardsByName = {}
##    for card in excelCards:
##        cardsByName[card.fileName] = card
##        
##    iStream = open(mappingFile, "r")
##    for rec in csv.DictReader(iStream):
##        if float(rec["Distance"]) > 90:
##            continue
##        mapping[rec["FNAME"]] = rec["ID_1"]
##        if rec["FNAME"] in cardsByName:
##            cardsByName[rec["FNAME"]].mappedNodeId = int(float(rec["ID_1"]))
##        else:
##            pass
##            #print rec["FNAME"]
##        
##    #excelCard.mappedNodeName = node.streetNames
##    #excelCard.mappedNodeId = node.iid
##
##    output = open("/Users/michalis/Dropbox/tmp/mappedCards.txt", "w")
##    #output = open("mappedCards.txt", "w") 
##    for card in excelCards:
##        if card.mappedNodeId:
##            output.write("%s\t%s\n" % (card.fileName, card.mappedNodeId))
##        else:
##            card.mappedNodeId = "-9999"
##            output.write("%s\t%s\n" % (card.fileName, card.mappedNodeId))            
##            
##    output.close()
##    return excelCards

##def getTestScenario(): 
##    """
##    return a test scenario
##    """
##    projectFolder = "/Users/michalis/Documents/workspace/dta/dev/testdata/dynameqNetwork_gearySubset"
##    prefix = 'smallTestNet' 
##
##    scenario = DynameqScenario(datetime.datetime(2010,1,1,0,0,0), 
##                               datetime.datetime(2010,1,1,4,0,0))
##    scenario.read(projectFolder, prefix) 
##
##    return scenario 

def assignCardNames(excelCards): 
       
    streetNames = extractStreetNames(excelCards.iName)
    excelCards.streetNames = sorted(cleanStreetNames(streetNames))
    excelCards.iiName = ",".join(excelCards.streetNames)
    
def mapIntersectionsByName(network, excelCards, mappedExcelCard, mappedNodes):
    """
    Map each excel card to a dynameq node
    """
    
    ## These sets are now created and called in the _main_ section 
    #mappedExcelCards = []

    #mappedNodes = {}
    #for sd in excelCards:

    if findNodeWithSameStreetNames(network, excelCards, 0.9, mappedNodes):
        mappedExcelCard = excelCards
        #dta.DtaLogger.info("Mapped %s to %d (%s)" % (sd.fileName, sd.mappedNodeId, str(network.getNodeForId(sd.mappedNodeId).getStreetNames())))
    else:
        dta.DtaLogger.error("Failed to map %s" % excelCards.fileName)


def getPossibleLinkDirections(link):
    """Return a two element tuple containing the possible directions of the link.
    Example (NB, WB)"""

    result = []

    orientation = link.getOrientation()
    if orientation >= 270 or orientation < 90:
        result.append("NB")
    else:
        result.append("SB")

    if orientation >= 0 and orientation < 180:
        result.append("EB")
    else:
        result.append("WB")

    return tuple(result)                                                                                        

def convertSignalToDynameq(net, node, card, planInfo, startTime, endTime):
    """
    Convert the excel signal described by the card object to
    a Dynameq time plan and return it. The input planInfo
    object determines the time period of operation
    """
## Commented this out so that start and end time can be specified based on type of signal while plan start and end are matched to scenario start and end
    #startTime, endTime = planInfo.getTimePeriod()
    signalTiming = selectCSO(card,startTime, endTime)
    if signalTiming:
        cso = signalTiming.cso
    if not signalTiming:
        cso = None

    if not cso:
        startTime = dta.Time(0,0)
        endTime = dta.Time(0,0)
        signalTiming = selectCSO(card,startTime, endTime)
        if signalTiming:
            cso = signalTiming.cso
        if not signalTiming:
            cso = None
        if not cso:
            dta.DtaLogger.error("Unable to find CSO for signal %s" % card.fileName) 
            raise dta.DtaError("Unable to find CSO for signal %s" % card.fileName)
    dta.DtaLogger.debug("Signal %s selected CSO %s with start time %s and end time %s." % (card.fileName, cso, signalTiming.startTime, signalTiming.endTime))
    offset = card.signalTiming[cso].offset
    dPlan = TimePlan(node, offset, planInfo)

    if startTime==dta.Time(0,0) and endTime == dta.Time(0,0):
        excelPhases = card.getPhases(startTime,endTime)
    else:
        #startTime, endTime= planInfo.getTimePeriod()
        excelPhases = card.getPhases(startTime, endTime)
    
    for excelPhase in excelPhases:

        groupMovemenents = excelPhase["Movs"]
        green = excelPhase["green"]
        yellow = excelPhase["yellow"]
        red = excelPhase["allRed"]

        dPhase = Phase(dPlan, green, yellow, red, Phase.TYPE_STANDARD)

        for groupMovement in groupMovemenents:
            dMovsAsStr = card.mappedMovements[groupMovement]
            for dMovStr in dMovsAsStr:
                n1, n2, n3 = map(int, dMovStr.split())
                dMov = node.getMovement(n1, n3)
                if dMov.isProhibitedToAllVehicleClassGroups():
                    continue
                
                #Warn about turning movements that have dedicated signal phases.  These are designated as PROTECTED below.
                if (dMov.getTurnType() in groupMovementToTurnType(groupMovement,addRightToThru=False)):
                    dta.DtaLogger.warn("PERMITTED should be PROTECTED? groupMovement=%s turntype=%s,  dMov=[%s %s] to [%s %s] turntype=%s" % \
                                       (groupMovement, groupMovementToTurnType(groupMovement), 
                                        dMov.getIncomingLink().getLabel(), 
                                        dMov.getIncomingLink().getDirection(),
                                        dMov.getOutgoingLink().getLabel(),
                                        dMov.getOutgoingLink().getDirection(), 
                                        dMov.getTurnType()))
                
                #Set through movements to be protected
                if dMov.isThruTurn():
                    phaseMovement = PhaseMovement(dMov, PhaseMovement.PROTECTED)
                #Set turn movements to be protected if there is a dedicated signal phase
                elif (dMov.getTurnType() in groupMovementToTurnType(groupMovement,addRightToThru=False)):
                    phaseMovement = PhaseMovement(dMov, PhaseMovement.PROTECTED)
                #Set all other movements to be permitted
                else:
                    phaseMovement = PhaseMovement(dMov, PhaseMovement.PERMITTED)
                ##TODO: Figure out which turn movements are protected from both other traffic AND conflicting pedestrians/cyclists (i.e. pedestrian scrambles and turn arrows)
                
                if not dPhase.hasPhaseMovement(phaseMovement.getMovement().getStartNodeId(),
                                               phaseMovement.getMovement().getEndNodeId()):                    
                    dPhase.addPhaseMovement(phaseMovement)
                
                # overrides
                if (dMov.getIncomingLink().getLabel()=="SAN ANSELMO AVE" and dMov.getIncomingLink().hasDirection(dta.RoadLink.DIR_WB) and
                    dMov.getOutgoingLink().getLabel()=="PORTOLA DR"      and dMov.getOutgoingLink().hasDirection(dta.RoadLink.DIR_WB)):
                    dPhase.addPhaseMovement(PhaseMovement(net.findMovementForRoadLabels("SANTA ANA AVE", dta.RoadLink.DIR_NB,
                                                                                        "PORTOLA DR",    dta.RoadLink.DIR_WB,
                                                                                        "PORTOLA DR",
                                                                                        use_dir_for_movement=False), PhaseMovement.PERMITTED))
                    dPhase.addPhaseMovement(PhaseMovement(net.findMovementForRoadLabels("SANTA ANA AVE", dta.RoadLink.DIR_NB,
                                                                                        "14TH AVE",      dta.RoadLink.DIR_WB,
                                                                                        "PORTOLA DR",
                                                                                        use_dir_for_movement=False), PhaseMovement.PERMITTED))

        dPlan.addPhase(dPhase)

    return dPlan
    
def manuallyDetermineMappedNodeId(net, cardsDirectory, manuallyMappedIntersectionsFile):

    fileNames = os.listdir(cardsDirectory)
    
    cards = parseExcelCardsToSignalObjects(cardsDirectory)
    assignCardNames(cards)
    cardsByName = {}
    for card in cards:
        cardsByName[card.fileName] = card

    result = []
    for record in csv.DictReader(open(manuallyMappedIntersectionsFile, "r")):
        if record["status"] == "2":
            nodeId = record["manualCubeNode"].strip()
            if nodeId:
                id_ = int(nodeId) 
                if net.hasNodeForId(id_):
                    cardsByName[record["fileName"]].mappedNodeId = int(nodeId)
                    result.append(cardsByName[record["fileName"]])
                else:
                    print "File ", record["fileName"], " does has been mapped to an non-existing node", id_
            else:
                print "File ", record["fileName"], " does not have a mapped node id"

    return result 

def getMappedCards(net, excelCards, mappedExcelCard, mappedNodes, cardsDirectoryManuallyMapped=None): 
    """
    Read the cards in the cards directory and map them to network nodes.
    Return the mapped signal objects cards in a list 
    """
    ## This is not needed anymore.  The parsed excel card is passed to the function as an argument.
    #excelCards = parseExcelCardsToSignalObjects(cardsDirectory)

    cards = excelCards
    assignCardNames(cards)
    mapIntersectionsByName(net, cards, mappedExcelCard, mappedNodes)

    if cardsDirectoryManuallyMapped:
        raise Exception("We should not have any manually mapped nodes") 
    if cardsDirectoryManuallyMapped:
        cards2 = manuallyDetermineMappedNodeId(net, cardsDirectoryManuallyMapped)
        assignCardNames(cards2)     
        cards3 = mapStreetNamesForManuallyMappedNodes(net, cards2)
        cards.extend(cards3)
        
    return cards 

def exportToJSON(cards, outFileName):
    """
    Export the signal cards to json format
    """
    output = open("outfileName.json", "w")
    for card in cards:
        output.write(json.dumps(card.toDict(),separators=(',',':'), indent=4))
    output.close()

def createDynameqSignals(net, card, planInfo,startTime, endTime):
    """
    Create a dynameq signal for each excel card object for
    the specified input period
    """
    #for card in cardsWithMovements:
    nodeId = card.mappedNodeId
    node = net.getNodeForId(nodeId)
    try:
        dPlan = convertSignalToDynameq(net, node, card, planInfo, startTime, endTime)
        dPlan.setPermittedMovements()            
        dPlan.validate()
                        
    except ExcelCardError, e:
        dta.DtaLogger.error("Error 1: %s" % e)
        return False
    except dta.DtaError, e:
        dta.DtaLogger.error("Error 2: %s" % e)
        return False
    try:
        node.addTimePlan(dPlan, raiseValidateError=True)
    except dta.DtaError, e:
        dta.DtaLogger.error("Error 3: %s" % e)
        return False

    # todo: remove these.  node.addTimePlan() set the control variable        
    assert(node._control == 1)
    node._control=1
    
    return dPlan

def verifySingleSignal(net, fileName, mappedNodes):
    """
    Read the signal card stored in the fileName and verify that
    can be imported properly to the dynameq network provided as
    the first argument 
    """
    directory, fn = os.path.split(fileName)
    sd = parseExcelCardFile(directory, fn)
    cards = [sd]
    assignCardNames(cards)
    mapIntersectionsByName(net, cards)

    if not sd.mappedNodeId:
        print "The card was not mapped to a Cube node" 
    else:
        node = net.getNodeForId(sd.mappedNodeId)
        print "%20s,%s" % ("Cube int name", node.getStreetNames())
        print "%20s,%s" % ("Excel card name", str(sd.streetNames))
        mapMovements(cards, net)

        for movDir, nodeTriplets in sd.mappedMovements.iteritems():
            print movDir            
            for nodeTriplet in nodeTriplets:
                print "\t\t%s" % nodeTriplet

if __name__ == "__main__":

    if len(sys.argv) < 6:
        print USAGE
        sys.exit(2)

    INPUT_DYNAMEQ_NET_DIR         = sys.argv[1]
    INPUT_DYNAMEQ_NET_PREFIX      = sys.argv[2]
    EXCEL_DIR                     = sys.argv[3]
    START_TIME                    = sys.argv[4]
    END_TIME                      = sys.argv[5]
    if len(sys.argv) >= 7:
        MOVEMENT_TURN_OVERRIDES   = sys.argv[6:]
    else:
        MOVEMENT_TURN_OVERRIDES   = None
    #OUTPUT_DYNAMEQ_NET_DIR        = sys.argv[6]
    #OUTPUT_DYNAMEQ_NET_PREFIX     = sys.argv[7]

    # The SanFrancisco network will use feet for vehicle lengths and coordinates, and miles for link lengths
    dta.VehicleType.LENGTH_UNITS= "feet"
    dta.Node.COORDINATE_UNITS   = "feet"
    dta.RoadLink.LENGTH_UNITS   = "miles"

    dta.setupLogging("importExcelSignals.INFO.log", "importExcelSignals.DEBUG.log", logToConsole=True)

    scenario = dta.DynameqScenario()
    scenario.read(INPUT_DYNAMEQ_NET_DIR, INPUT_DYNAMEQ_NET_PREFIX) 
    net = dta.DynameqNetwork(scenario)
    net.read(INPUT_DYNAMEQ_NET_DIR, INPUT_DYNAMEQ_NET_PREFIX)
    
    if MOVEMENT_TURN_OVERRIDES:
        overrides = []
        for override_file in MOVEMENT_TURN_OVERRIDES:
            inputstream = open(override_file, "r")
            for line in inputstream:
                line = line.strip("\n")
                override = line.split(",")
                if override[0] == "From Dir": continue # header line
                if override[5] == 'Thru': override[5] = dta.Movement.DIR_TH
                overrides.append(override)
        net.setMovementTurnTypeOverrides(overrides)


    for node in net.iterRoadNodes():
        node._control = 0
    ## This section was testing the pickle module, but it's not used anymore.      
##    in2 = open("test.pkl", "rb")
##    data2 = pickle.load(in2)
##    in2.close()    

##    for i in range(len(excelCards)):
##        excelCards[i].mappedStreet = data2[i][0]
##        excelCards[i].mappedNodeName = data2[i][1]
##        excelCards[i].mappedNodeId = data2[i][2]
    problemCards = []
    cardsDone = []
    mappedNodes = {}
    cardsWithMove = []
    allPlansSet=[]
    allMoreMatchesSet=[]
    nummatches = 0
    planInfo = net.addPlanCollectionInfo(dta.Time.readFromString(scenario.startTime.strftime("%H:%M")), dta.Time.readFromString(scenario.endTime.strftime("%H:%M")), "excelSignalsToDynameq", "excel_signal_cards_imported_automatically")

    for fileName in os.listdir(EXCEL_DIR):
        excelCards = parseExcelCardsToSignalObjects(EXCEL_DIR,fileName)
        if excelCards == False:
            continue
        else:
            cardsDone.append(excelCards)
        assignCardNames(excelCards)
        
        cards = excelCards
        mappedExcelCard = []
        cards = getMappedCards(net, excelCards, mappedExcelCard, mappedNodes) 
    
        cardsWithMovements = mapMovements(cards, net)
        if cardsWithMovements == False:
            continue
        else:
            cardsWithMove.append(cardsWithMovements)           
        allPlans = createDynameqSignals(net, cardsWithMovements, planInfo, dta.Time.readFromString(START_TIME), dta.Time.readFromString(END_TIME))

        if allPlans == False:
            continue
        else:
            allPlansSet.append(allPlans)
        ## This section is used to check for cards that have multiple CSOs matching the start and and time.  This allows us to identify
        ## cards that have both weekend and weekday time plans so that we know which ones need fixed.
        nummatches = checkNumberofTimes(cardsWithMovements, dta.Time.readFromString(START_TIME), dta.Time.readFromString(END_TIME))
        if nummatches>1:
            allMoreMatchesSet.append(fileName)
            dta.DtaLogger.error("Signal %s has %d phases matching the start and end time" % (fileName, nummatches))

    dta.DtaLogger.info("Number of excel cards successfully parsed = %d" % len(cardsDone))
    dta.DtaLogger.info("Number of cards are %d; Number of mapped nodes are %d" % (len(cardsDone), len(mappedNodes)))
    dta.DtaLogger.info("Number of cards are %d; Number of cards with movements are %d" % (len(cardsDone),len(cardsWithMove)))
    dta.DtaLogger.info("Number of time plans = %d" % len(allPlansSet))
    dta.DtaLogger.info("Number of excel cards with multiple times matching start and end time = %d" % len(allMoreMatchesSet))

    #Adjust Followup time for signalized right and left turning movements 
    rightTurnFollowupLookup = {90:2.22, 91:2.11, 92:2.0, 93:2.0, 94:2.0, 95:2.0,}
    leftTurnFollowupLookup  = {90:2.22, 91:2.11, 92:2.0, 93:2.0, 94:2.0, 95:2.0,} 
    
    for node in net.iterRoadNodes():
        for mov in node.iterMovements():
            if not node.hasTimePlan():
                mov.setFollowup(-1)
                continue
            if mov.isRightTurn():
                mov.setFollowup(str(rightTurnFollowupLookup[mov.getFollowup()]))
            elif mov.isLeftTurn():
                mov.setFollowup(str(leftTurnFollowupLookup[mov.getFollowup()]))
            else:
                mov.setFollowup(-1)

    net.write(".", "sf_signals")

    #net.writeLinksToShp("sf_signals_link")
    #net.writeNodesToShp("sf_signals_node")    
    
