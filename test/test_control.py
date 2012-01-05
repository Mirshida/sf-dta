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
import os
import datetime

from dta.Scenario import Scenario
from dta.DynameqScenario import DynameqScenario 
from dta.Network import Network
from dta.DtaError import DtaError 
from dta.DynameqNetwork import DynameqNetwork 
from dta.TimePlan import TimePlan

mainFolder = "/Users/michalis/Documents/workspace/dta/dev/testdata"

def getTestScenario(): 

    projectFolder = os.path.join(mainFolder, 'dynameqNetwork_gearySubset')
    prefix = 'smallTestNet' 

    scenario = DynameqScenario(datetime.datetime(2010,1,1,0,0,0), datetime.datetime(2010,1,1,4,0,0))
    scenario.read(projectFolder, prefix) 

    return scenario 

def getGearyNet():

    gearynetDta = DynameqNetwork(scenario=getTestScenario())
    gearynetDta.read(dir="/Users/michalis/Documents/workspace/dta/dev/testdata/dynameqNetwork_geary", file_prefix="Base")
    
    return gearynetDta

class TestControl:


    def test_one(self):

        net = getGearyNet()
        fileName = "/Users/michalis/Documents/workspace/dta/dev/testdata/dynameqNetwork_geary/Base_ctrl.dqt"       
        for tp in TimePlan.read(net, fileName):
            tp.getNode().addTimePlan(tp)
        
        projectFolder = "/Users/michalis/Documents/workspace/dta/dev/testdata/dynameqNetwork_geary/test"       
        net.write(dir=projectFolder, file_prefix="Test")