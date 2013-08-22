
"""
DagmanDataWorkflow,

A module providing HTCondor querying capabilities to the CRABServer
"""
# First, see if we have python condor libraries
try:
    import htcondor
except ImportError:
    htcondor = None #pylint: disable=C0103
try:
    import WMCore.BossAir.Plugins.RemoteCondorPlugin as RemoteCondorPlugin
except ImportError:
    if not htcondor:
        raise

import os
import os.path
import subprocess
import re
import json
import time
import errno
import logging
import traceback
import pprint
import sys
import TaskWorker
import CRABInterface.DataWorkflow
import TaskWorker.Actions.DagmanSubmitter

from WMCore.Configuration import Configuration
from WMCore.REST.Error import InvalidParameter
from CRABInterface.Utils import retrieveUserCert
from CRABInterface.CRABServerBase import getCRABServerBase
from Databases.CAFUtilitiesBase import getCAFUtilitiesBase
# Sorry for the wildcard
from CRABInterface.DagmanSnippets import *
from TaskWorker.Actions.DagmanCreator import escape_strings_to_classads

def getCRABInfoFromClassAd(ad):
    info = {}
    for adName, dictName in SUBMIT_INFO:
        pass

class DagmanDataWorkflow(CRABInterface.DataWorkflow.DataWorkflow):
    """A specialization of the DataWorkflow for submitting to HTCondor DAGMan instead of
       PanDA
    """
    JOB_KILLED_HOLD_REASON='Killed by CRAB3 client'
    JOB_RESTART_HOLD_REASON='Restarted by CRAB3 client'

    def __init__(self, **kwargs):
        print "got initial kwargs %s" % kwargs
        super(DagmanDataWorkflow, self).__init__()
        self.config = None
        if 'config' in kwargs:
            self.config = kwargs['config']
        else:
            self.logger.error("A config wasn't passed to DagmanDataWorkflow")
            self.config = Configuration()
        
        self.config.section_("BossAir")
        self.config.section_("General")
        if not hasattr(self.config.BossAir, "remoteUserHost"):
            # Not really the best place for a default, I think...
            # TODO: Also, this default should pull from somewhere smarter
            self.config.BossAir.remoteUserHost = "submit-5.t2.ucsd.edu"
        self.logger.debug("Got kwargs %s " % kwargs)
        if 'requestarea' in kwargs:
            self.requestarea = kwargs['requestarea']
        else:
            self.requestarea = "/tmp/crab3"
        self.logger.debug("Setting request area to %s" % self.requestarea)


    def getBinDir(self):
        """
        Returns the directory of pithy shell scripts
        TODO this is definitely a thing that needs to be fixed for an RPM-deploy
        """
        # TODO: Nuke this with the rest of the dev hooks
        binDir = os.path.join(getCRABServerBase(), "bin")
        if self.config and hasattr(self.config.General, 'binDir'): #pylint: disable=E1103
            binDir = self.config.General.binDir #pylint: disable=E1103
        if 'CRAB3_BASEPATH' in os.environ:
            binDir = os.path.join(os.environ["CRAB3_BASEPATH"], "bin")
        return os.path.expanduser(binDir)


    def getTransformLocation(self):
        """
        Returns the location of the PanDA job transform
        """
        # TODO: Nuke this with the rest of the dev hooks
        tDir = os.path.join(getCAFUtilitiesBase(), "src", "python", \
                    "transformation")
        if self.config and hasattr(self.config.General, 'transformDir'): #pylint: disable=E1103
            tDir = self.config.General.transformDir #pylint: disable=E1103
        if 'CRAB3_BASEPATH' in os.environ:
            tDir = os.path.join(os.environ["CRAB3_BASEPATH"], "bin")
        return os.path.join(os.path.expanduser(tDir), "CMSRunAnalysis.sh")

    def getRemoteCondorSetup(self):
        """
        Returns the environment setup file for the remote schedd.
        """
        return ""

    # NOTE NOTE NOTE
    # The following function gets wrapped in a proxy decorator below
    # the wrapped one is called submit (imagine that)
    def submitUnwrapped(self, workflow, jobtype, jobsw, jobarch, inputdata, siteblacklist, sitewhitelist, splitalgo, algoargs, cachefilename, cacheurl, addoutputfiles, \
               userhn, userdn, savelogsflag, publishname, asyncdest, blacklistT1, dbsurl, publishdbsurl, vorole, vogroup, tfileoutfiles, edmoutfiles, runs, lumis, userproxy=None, testSleepMode=False, **kwargs):
        """Perform the workflow injection into the reqmgr + couch

           :arg str workflow: workflow name requested by the user;
           :arg str jobtype: job type of the workflow, usually Analysis;
           :arg str jobsw: software requirement;
           :arg str jobarch: software architecture (=SCRAM_ARCH);
           :arg str inputdata: input dataset;
           :arg str list siteblacklist: black list of sites, with CMS name;
           :arg str list sitewhitelist: white list of sites, with CMS name;
           :arg str list blockblacklist:  input blocks to be excluded from the specified input dataset;
           :arg str splitalgo: algorithm to be used for the workflow splitting;
           :arg str algoargs: argument to be used by the splitting algorithm;
           :arg str list addoutputfiles: list of additional output files;
           :arg int savelogsflag: archive the log files? 0 no, everything else yes;
           :arg str userdn: DN of user doing the request;
           :arg str userhn: hyper new name of the user doing the request;
           :arg str publishname: name to use for data publication;
           :arg str asyncdest: CMS site name for storage destination of the output files;
           :arg int blacklistT1: flag enabling or disabling the black listing of Tier-1 sites;
           :arg str dbsurl: dbs url where the input dataset is published;
           :arg str publishdbsurl: dbs url where the output data has to be published;
           :arg str list runs: list of run numbers
           :arg str list lumis: list of lumi section numbers
           :arg bool testSleepMode: If true, the condor job will just run 'sleep'
           :returns: a dict which contaians details of the request"""

        self.logger.debug("""workflow %s, jobtype %s, jobsw %s, jobarch %s, inputdata %s, siteblacklist %s, sitewhitelist %s, 
               splitalgo %s, algoargs %s, cachefilename %s, cacheurl %s, addoutputfiles %s, savelogsflag %s,
               userhn %s, publishname %s, asyncdest %s, blacklistT1 %s, dbsurl %s, publishdbsurl %s, tfileoutfiles %s, edmoutfiles %s, userdn %s,
               runs %s, lumis %s"""%(workflow, jobtype, jobsw, jobarch, inputdata, siteblacklist, sitewhitelist, \
               splitalgo, algoargs, cachefilename, cacheurl, addoutputfiles, savelogsflag, \
               userhn, publishname, asyncdest, blacklistT1, dbsurl, publishdbsurl, tfileoutfiles, edmoutfiles, userdn, \
               runs, lumis))

        # Esp for unittesting, you can get the same timestamp
        timestamp = time.strftime('%y%m%d_%H%M%S', time.gmtime())

        dagmanSubmitter = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        scheddName = dagmanSubmitter.getSchedd()

        requestname = '%s_%s_%s_%s' % (scheddName, timestamp, userhn, workflow)
        if self.requestarea == '/tmp/crab3':
            raise
        scratch = os.path.join(self.requestarea, "." + requestname)
        os.makedirs(scratch)

        # Poor-man's string escaping.  We do this as classad module isn't guaranteed to be present.
        # FIXME: Hack because I think bbockelm forgot to commit the available_sites change
        available_sites = ['T2_US_Nebraska']
        info = escape_strings_to_classads(locals())
        # Condor will barf if the requesname is quoted .. for some reason
        info['requestname'] = info['requestname'].replace('"','')
        info['remote_condor_setup'] = self.getRemoteCondorSetup()
        info['bindir'] = self.getBinDir()
        info['transform_location'] = self.getTransformLocation()
        
        if kwargs.get('taskManagerTarball', None) and \
                kwargs.get('taskManagerCodeLocation', None) and \
                kwargs['taskManagerTarball'] != 'local':
            raise RuntimeError, "Debug.taskManagerTarball must be 'local' if you provide a code location"

        if kwargs.get('taskManagerCodeLocation', None):
            kwargs['taskManagerTarball'] = 'local'

        if kwargs.get('taskManagerTarball', None):
            info['additional_environment_options'] = 'CRAB_TASKMANAGER_TARBALL=%s' % kwargs['taskManagerTarball']
        else:
            # need to have  a blank variable to keep the separaters right
            info['additional_environment_options'] = 'DUMMY=""'
        info['additional_input_files'] = ''
        if kwargs.get('taskManagerTarball') == 'local':
            if not kwargs.get('taskManagerCodeLocation', None):
                raise RuntimeError, "Tarball was set to local, but not location was set"
            # we need to generate the tarball to ship along with the jobs
            #  CAFTaskWorker/bin/dagman_make_runtime.sh
            self.logger.info('Packing up tarball from local source tree')
            runtimePath = os.path.expanduser( os.path.join( 
                                        kwargs['taskManagerCodeLocation'],
                                        'CAFTaskWorker',
                                        'bin',
                                        'dagman_make_runtime.sh') )
            tarMaker = subprocess.Popen([ runtimePath, kwargs['taskManagerCodeLocation']])
            tarMaker.communicate()
            info['additional_input_files'] = ', TaskManagerRun.tar.gz'
                                          

        schedd, address = dagmanSubmitter.getScheddObj(scheddName)

        with open(os.path.join(scratch, "master_dag"), "w") as fd:
            fd.write(MASTER_DAG_FILE % info)
        with open(os.path.join(scratch, "DBSDiscovery.submit"), "w") as fd:
            fd.write(DBS_DISCOVERY_SUBMIT_FILE % info)
        with open(os.path.join(scratch, "JobSplitting.submit"), "w") as fd:
            fd.write(JOB_SPLITTING_SUBMIT_FILE % info)
        with open(os.path.join(scratch, "Job.submit"), "w") as fd:
            fd.write(JOB_SUBMIT % info)
        with open(os.path.join(scratch, 'ASO.submit'), 'w') as fd:
            fd.write(ASYNC_SUBMIT % info)

        inputFiles = [os.path.join(self.getBinDir(), "dag_bootstrap.sh"),
		       os.path.join(self.getBinDir(), "dag_bootstrap_startup.sh"),
                       self.getTransformLocation(),
                       os.path.join(self.getBinDir(), "cmscp.py")]
        scratch_files = ['master_dag', 'DBSDiscovery.submit', 'JobSplitting.submit', 'master_dag', 'Job.submit', 'ASO.submit']
        inputFiles.extend([os.path.join(scratch, i) for i in scratch_files])
        if kwargs.get('taskManagerTarball', None) == 'local':
            inputFiles.append('TaskManagerRun.tar.gz')

        outputFiles = ["master_dag.dagman.out", "master_dag.rescue.001", "RunJobs.dag",
            "RunJobs.dag.dagman.out", "RunJobs.dag.rescue.001", "dbs_discovery.err",
            "dbs_discovery.out", "job_splitting.err", "job_splitting.out"]

        info['testSleepMode'] = kwargs.get('testSleepMode', False)

        if address:
            self.logger.info("Submitting directly to HTCondor (via python bindings)")
            # Submit directly to a scheduler
            info['outputFilesString'] = ", ".join(outputFiles)
            info['inputFilesString'] = ", ".join(inputFiles)

            dagmanSubmitter.submitDirect(schedd,
                os.path.join(self.getBinDir(), "dag_bootstrap_startup.sh"), 'master_dag',
                info)
        else:
            # Submit over Gsissh
            # testing getting the right directory
            self.logger.info("Submitting remotely to HTCondor (via gsissh)")
            requestname_bak = requestname
            requestname = './'
            info['outputFilesString'] = ", ".join([os.path.basename(x) for x in outputFiles])
            info['inputFilesString']  = ", ".join([os.path.basename(x) for x in inputFiles])
            info['iwd'] = '%s/' % requestname_bak
            info['scratch'] = '%s/' % requestname
            info['bindir'] = '%s/' % requestname
            info['transform_location'] = os.path.basename(info['transform_location'])
            info['x509up_file'] = '%s/user.proxy' % requestname
            info['userproxy'] = '%s/user.proxy' % requestname
            info['configdoc'] = 'CONFIGDOCSUP'
            jdl = MASTER_DAG_SUBMIT_FILE % info
            with open(os.path.join(scratch, 'submit.jdl'), 'w') as fd:
                fd.write(jdl)
            requestname = requestname_bak
            #schedd.submitRaw(requestname, os.path.join(scratch, 'submit.jdl'), info['userproxy'], inputFiles)
            schedd.submitRaw(requestname, os.path.join(scratch, 'submit.jdl'), userproxy, inputFiles)
        return [{'RequestName': requestname}]


    # NOTE: Tricky bit that makes submit = the wrapped submit function 
    submit = retrieveUserCert(submitUnwrapped)


    @retrieveUserCert
    def kill(self, workflow, force, userdn, **kwargs):
        """Request to Abort a workflow.

           :arg str workflow: a workflow name"""

        self.logger.info("About to kill workflow: %s. Getting status first." % workflow)

        userproxy = kwargs['userproxy']
        workflow = str(workflow)
        if not WORKFLOW_RE.match(workflow):
            raise Exception("Invalid workflow name.")

        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        scheddName = dag.getSchedd()
        schedd, address = dag.getScheddObj(scheddName)

        const = 'TaskType =?= \"ROOT\" && CRAB_ReqName =?= "%s" && CRAB_UserDN =?= "%s"' % (workflow, userdn)
        if address:
            r, w = os.pipe()
            rpipe = os.fdopen(r, 'r')
            wpipe = os.fdopen(w, 'w')
            if os.fork() == 0:
                try:
                    rpipe.close()
                    try:
                        htcondor.SecMan().invalidateAllSessions()
                        os.environ['X509_USER_PROXY'] = userproxy
                        schedd.edit(const, "HoldReason", "\"%s\"" % self.JOB_KILLED_HOLD_REASON)
                        schedd.act(htcondor.JobAction.Hold, const)
                        schedd.edit(const, "HoldReason", "\"%s\"" % self.JOB_KILLED_HOLD_REASON)
                        wpipe.write("OK")
                        wpipe.close()
                        os._exit(0)
                    except Exception, e:
                        wpipe.write(str(traceback.format_exc()))
                finally:
                    wpipe.close()
                    os._exit(1)
            wpipe.close()
            results = rpipe.read()
            if results != "OK":
                raise Exception("Failure when submitting to HTCondor: %s" % results)
        else:
            # Use the remoteCondor plugin 
            schedd.hold(const) #pylint: disable=E1103

        # Search for and hold the sub-dag
        rootConst = "TaskType =?= \"ROOT\" && CRAB_ReqName =?= \"%s\" && (isUndefined(CRAB_Attempt) || CRAB_Attempt == 0)" % workflow
        rootAttrList = ["ClusterId"]
        if address:
            results = schedd.query(rootConst, rootAttrList)
        else:
            results = schedd.getClassAds(rootConst, rootAttrList)

        if not results:
            return

        subDagConst = "DAGManJobId =?= %s && DAGParentNodeNames =?= \"JobSplitting\"" % results[0]["ClusterId"]
        if address:
            subDagResults = schedd.query(subDagConst, rootAttrList)
        else:
            subDagResults = schedd.getClassAds(subDagConst, rootAttrList)

        if not subDagResults:
            return
        finished_jobConst = "DAGManJobId =?= %s && ExitCode =?= 0" % subDagResults[0]["ClusterId"]
        if address:
            r, w = os.pipe()
            childPid = os.fork()
            if childPid == 0:
                try:
                    os.close(r)
                    wpipe = os.fdopen(w, 'w')
                    try:
                        htcondor.SecMan().invalidateAllSessions()
                        os.environ['X509_USER_PROXY'] = userproxy
                        schedd.edit(subDagConst, "HoldKillSig", "\"SIGUSR1\"")
                        schedd.act(htcondor.JobAction.Hold, subDagConst)
                        schedd.edit(finished_jobConst, "DAGManJobId", "-1")
                        wpipe.write("OK")
                        wpipe.close()
                        os._exit(0)
                    except Exception, e:
                        print str(traceback.format_exc())
                        wpipe.write(str(traceback.format_exc()))
                finally:
                    wpipe.close()
                    os._exit(1)
            os.close(w)
            os.waitpid(childPid, 0)
            rpipe = os.fdopen(r, 'r')
            results = rpipe.read()
            if results != "OK":
                raise Exception("Failure when killing job: %s" % results)
        else:
            # Use the remoteCondor plugin
            schedd.edit(subDagConst, "HoldKillSig", "SIGUSR1")
            schedd.hold(const) #pylint: disable=E1103

    def getScheddAndAddress(self):
        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        scheddName = dag.getSchedd()
        return  dag.getScheddObj(scheddName)

    def getRootTasks(self, workflow, schedd):
        rootConst = "TaskType =?= \"ROOT\" && CRAB_ReqName =?= \"%s\" && (isUndefined(CRAB_Attempt) || CRAB_Attempt == 0)" % workflow
        rootAttrList = ["JobStatus", "ExitCode", 'CRAB_JobCount', 'CRAB_ReqName', 'TaskType', "HoldReason"]
        #print "Using rootConst: %s" % rootConst
        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        scheddName = dag.getSchedd()
        schedd, address = dag.getScheddObj(scheddName)

        if address:
            results = schedd.query(rootConst, rootAttrList)
        else:
            results = schedd.getClassAds(rootConst, rootAttrList)

        if not results:
            self.logger.info("An invalid workflow name was requested: %s" % workflow)
            self.logger.info("Tried to read from address %s" % address)
            raise InvalidParameter("An invalid workflow name was requested: %s" % workflow)
        return results


    def getASOJobs(self, workflow, schedd):
        jobConst = "TaskType =?= \"ASO\" && CRAB_ReqName =?= \"%s\"" % workflow
        jobList = ["JobStatus", 'ExitCode', 'ClusterID', 'ProcID', 'CRAB_Id']
        _, address = self.getScheddAndAddress()
        if address:
            results = schedd.query(jobConst, jobList)
        else:
            results = schedd.getClassAds(jobConst, jobList)
        return results

    def getJobs(self, workflow, schedd):
        jobConst = "TaskType =?= \"Job\" && CRAB_ReqName =?= \"%s\"" % workflow
        jobList = ["JobStatus", 'ExitCode', 'ClusterID', 'ProcID', 'CRAB_Id']
        _, address = self.getScheddAndAddress()
        if address:
            results = schedd.query(jobConst, jobList)
        else:
            results = schedd.getClassAds(jobConst, jobList)
        return results

    def serializeFinishedJobs(self, workflow, userdn, userproxy=None):
        if not userproxy:
            return
        jobdir = os.path.join(self.requestdir, "."+workflow, "finished_condor_jobs")
        jobdir = os.abspath(jobdir)
        requestdir = os.abspath(self.requestdir)
        assert(jobdir.startswith(requestdir))
        try:
            os.makedirs(jobdir)
        except OSError, oe:
            if oe.errno != errno.EEXIST:
                raise

        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        schedd, address = dag.getScheddObj(name)

        finished_jobs = {}

        jobs = self.getJobs(workflow, schedd)
        jobs.extend(self.getASOJobs(workflow, schedd))
        for job in jobs:
            if 'CRAB_Id' not in job: continue
            if (int(job.get("JobStatus", "1")) == 4) and (task_is_finished or (int(job.get("ExitCode", -1)) == 0)):
                finished_jobs["%s.%s" % (job['ClusterID'], job['ProcID'])] = job

        # TODO: consider POSIX data integrity
        for id, job in finished_jobs.items():
            with open(os.path.join(jobdir, id), "w") as fd:
                json.dump(dict(job), fd)

        if address:
            schedd.act(htcondor.JobAction.Remove, finished_jobs.keys())
        else:
            schedd.remove(finished_jobs.keys())


    def getReports(self, workflow, userproxy):
        if not userproxy: return

        jobdir = os.path.join(self.requestdir, "."+workflow, "job_results")
        jobdir = os.abspath(jobdir)
        requestdir = os.abspath(self.requestdir)
        assert(jobdir.startswith(requestdir))
        try:
            os.makedirs(jobdir)
        except OSError, oe:
            if oe.errno != errno.EEXIST:
                raise

        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        schedd, address = dag.getScheddObj(name)

        job_ids = {}
        aso_ids = set()
        for job in self.getJobs(workflow, schedd):
            if 'CRAB_Id' not in job: continue
            if int(job.get("JobStatus", "1")) == 4:
                job_ids[int(job['CRAB_Id'])] = "%s.%s" % (job['ClusterID'], job['ProcID'])

        if address:
            schedd.edit(job_ids.values(), "TransferOutputRemaps", 'strcat("jobReport.json.", CRAB_Id, "=%s/jobReport.json.", CRAB_Id, ";job_out.", CRAB_Id, "=/dev/null;job_err.", CRAB_Id, "=/dev/null")' % jobdir)
            schedd.retrieve(job_ids.values())


    def status(self, workflow, userdn, userproxy=None):
        """Retrieve the status of the workflow

           :arg str workflow: a valid workflow name
           :return: a workflow status summary document"""
        workflow = str(workflow)
        if not WORKFLOW_RE.match(workflow):
            raise Exception("Invalid workflow name: %s" % workflow)
        
        name = workflow.split("_")[0]
        self.logger.debug("Getting status for workflow %s, looking for schedd %s" %\
                                (workflow, name))
        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        schedd, address = dag.getScheddObj(name)

        results = self.getRootTasks(workflow, schedd)
        #print "Got root tasks: %s" % results
        jobsPerStatus = {}
        jobStatus = {}
        jobList = []
        taskStatusCode = int(results[-1]['JobStatus'])
        taskJobCount = int(results[-1].get('CRAB_JobCount', 0))
        codes = {1: 'Idle', 2: 'Running', 4: 'Completed', 5: 'Killed'}
        retval = {"status": codes.get(taskStatusCode, 'Unknown'), "taskFailureMsg": "", "jobSetID": workflow,
            "jobsPerStatus" : jobsPerStatus, "jobList": jobList}
        if taskStatusCode == 5 and \
                results[-1]['HoldReason'] != self.JOB_KILLED_HOLD_REASON:
            retval['status'] = 'InTransition'

        failedJobs = []
        allJobs = self.getJobs(workflow, schedd)
        for result in allJobs:
            # print "Examining one job: %s" % result
            jobState = int(result['JobStatus'])
            if result['CRAB_Id'] in failedJobs:
                failedJobs.remove(result['CRAB_Id'])
            if (jobState == 4) and ('ExitCode' in result) and (int(result['ExitCode'])):
                failedJobs.append(result['CRAB_Id'])
                statusName = "Failed (%s)" % result['ExitCode']
            else:
                statusName = codes.get(jobState, 'Unknown')
            jobStatus[int(result['CRAB_Id'])] = statusName

        aso_codes = {1: 'ASO Queued', 2: 'ASO Running', 4: 'Stageout Complete (Success)'}
        for result in self.getASOJobs(workflow, schedd):
            if result['CRAB_Id'] in failedJobs:
                failedJobs.remove(result['CRAB_Id'])
            jobState = int(result['JobStatus'])
            if (jobState == 4) and ('ExitCode' in result) and (int(result['ExitCode'])):
                failedJobs.append(result['CRAB_Id'])
                statusName = "Failed Stage-Out (%s)" % result['ExitCode']
            else:
                statusName = aso_codes.get(jobState, 'Unknown')
            jobStatus[int(result['CRAB_Id'])] = statusName

        for i in range(1, taskJobCount+1):
            if i not in jobStatus:
                if taskStatusCode == 5:
                    jobStatus[i] = 'Killed'
                else:
                    jobStatus[i] = 'Unsubmitted'

        for job, status in jobStatus.items():
            jobsPerStatus.setdefault(status, 0)
            jobsPerStatus[status] += 1
            jobList.append((status, job))

        retval["failedJobdefs"] = len(failedJobs)
        retval["totalJobdefs"] = len(jobStatus)

        if len(jobStatus) == 0 and taskJobCount == 0 and taskStatusCode == 2:
            retval['status'] = 'Running (jobs not submitted)'

        retval['jobdefErrors'] = []

        self.logger.info("Status result for workflow %s: %s" % (workflow, retval))
        #print "Status result for workflow %s: %s" % (workflow, retval)

        return retval


    def outputLocation(self, workflow, maxNum, _):
        """
        Retrieves the output LFN from async stage out

        :arg str workflow: the unique workflow name
        :arg int maxNum: the maximum number of output files to retrieve
        :return: the result of the view as it is."""
        workflow = str(workflow)
        if not WORKFLOW_RE.match(workflow):
            raise Exception("Invalid workflow name.")

        name = workflow.split("_")[0]
        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        schedd, address = dag.getScheddObj(name)

        jobConst = 'TaskType =?= \"ASO\"&& CRAB_ReqName =?= \"%s\"' % workflow
        jobList = ["JobStatus", 'ExitCode', 'ClusterID', 'ProcID', 'GlobalJobId', 'OutputSizes', 'OutputPFNs']

        if address:
            results = schedd.query(jobConst, jobList)
        else:
            results = schedd.getClassAds(jobConst, jobList)
        files = []
        for result in results:
            try:
                outputSizes = [int(i.strip()) for i in result.get("OutputSizes", "").split(",") if i]
            except ValueError:
                self.logger.info("Invalid OutputSizes (%s) for workflow %s" % (result.get("OutputSizes", ""), workflow))
                raise InvalidParameter("Internal state had invalid OutputSize.")
            outputFiles = [i.strip() for i in result.get("OutputPFNs", "").split(",")]
            for idx in range(min(len(outputSizes), len(outputFiles))):
                files.append({'pfn': outputFiles[idx], 'size': outputSizes[idx]})

        if maxNum > 0:
            return {'result': files[:maxNum]}
        return {'result': files}

    @retrieveUserCert
    def resubmit(self, workflow, siteblacklist, sitewhitelist, userdn, **kwargs):
        # TODO: In order to take advantage of the updated white/black list, we need
        # to sneak those into the resubmitted DAG.

        self.logger.info("About to resubmit workflow: %s." % workflow)
        userproxy = kwargs['userproxy']
        workflow = str(workflow)
        if not WORKFLOW_RE.match(workflow):
            raise Exception("Invalid workflow name.")

        dag = TaskWorker.Actions.DagmanSubmitter.DagmanSubmitter(self.config)
        scheddName = dag.getSchedd()
        schedd, address = dag.getScheddObj(scheddName)

        # Search for and hold the sub-dag
        rootConst = "TaskType =?= \"ROOT\" && CRAB_ReqName =?= \"%s\" && (isUndefined(CRAB_Attempt) || CRAB_Attempt == 0)" % workflow
        rootAttrList = ["ClusterId"]
        if address:
            results = schedd.query(rootConst, rootAttrList)
        else:
            results = schedd.getClassAds(rootConst, rootAttrList)

        if not results:
            raise RuntimeError, "Couldn't find root dagman job to resubmit. It may have been too long"

        subDagConst = "DAGManJobId =?= %s && DAGParentNodeNames =?= \"JobSplitting\"" % results[0]["ClusterId"]
        if address:
            self.logger.debug('Resubmitting via python bindings')
            r, w = os.pipe()
            rpipe = os.fdopen(r, 'r')
            wpipe = os.fdopen(w, 'w')
            if os.fork() == 0:
                try:
                    rpipe.close()
                    try:
                        htcondor.SecMan().invalidateAllSessions()
                        os.environ['X509_USER_PROXY'] = userproxy
                        # change the hold reason so status calls will see InTransition
                        # and not Killed
                        print "PARTY"
                        print "const is %s" % rootConst
                        print "const is %s" % subDagConst
                        print schedd.act(htcondor.JobAction.Release, subDagConst)
                        print schedd.act(htcondor.JobAction.Release, rootConst)
                        print schedd.edit(rootConst, "HoldReason", "\"%s\"" % self.JOB_RESTART_HOLD_REASON)
                        try:
                            print schedd.edit(subDagConst, "HoldReason", "\"%s\"" % self.JOB_RESTART_HOLD_REASON)
                        except:
                            # The above blows up if there's no matching jobs.
                            # worst case is some jobs show up as killed temporarily
                            pass
                        print schedd.act(htcondor.JobAction.Release, subDagConst)
                        print schedd.act(htcondor.JobAction.Release, rootConst)

                        wpipe.write("OK")
                        wpipe.close()
                        os._exit(0)
                    except Exception:
                        wpipe.write(str(traceback.format_exc()))
                finally:
                    wpipe.close()
                    os._exit(1)
            os.wait()
            wpipe.close()
            results = rpipe.read()
            if results != "OK":
                raise Exception("Failure when resubmitting job: %s" % results)
        else:
            self.logger.debug('Resubmitting via gsissh')
            schedd.release(subDagConst) #pylint: disable=E1103
            schedd.release(rootConst) #pylint: disable=E1103

    def report(self, workflow, userdn, userproxy=None):

        self.getReports(workflow, userdn, userproxy)

        #load the lumimask
        rows = self.api.query(None, None, ID.sql, taskname = workflow)
        splitArgs = literal_eval(rows.next()[6].read())
        res['lumiMask'] = buildLumiMask(splitArgs['runs'], splitArgs['lumis'])

        #extract the finished jobs from filemetadata
        jobids = [x[1] for x in statusRes['jobList'] if x[0] in ['finished']]
        rows = self.api.query(None, None, GetFromPandaIds.sql, types='EDM', taskname=workflow, jobids=','.join(map(str,jobids)),\
                                        limit=len(jobids)*100)
        res['runsAndLumis'] = {}
        for row in rows:
            res['runsAndLumis'][str(row[GetFromPandaIds.PANDAID])] = { 'parents' : row[GetFromPandaIds.PARENTS].read(),
                    'runlumi' : row[GetFromPandaIds.RUNLUMI].read(),
                    'events'  : row[GetFromPandaIds.INEVENTS],
            }

        yield res


def main():
    dag = DagmanDataWorkflow()
    workflow = 'bbockelm'
    jobtype = 'analysis'
    jobsw = 'CMSSW_5_3_7'
    jobarch = 'slc5_amd64_gcc462'
    inputdata = '/GenericTTbar/HC-CMSSW_5_3_1_START53_V5-v1/GEN-SIM-RECO'
    siteblacklist = []
    sitewhitelist = ['T2_US_Nebraska']
    splitalgo = "LumiBased"
    algoargs = 40
    cachefilename = 'default.tgz'
    cacheurl = 'https://voatlas178.cern.ch:25443'
    addoutputfiles = []
    savelogsflag = False
    userhn = 'bbockelm'
    publishname = ''
    asyncdest = 'T2_US_Nebraska'
    blacklistT1 = True
    dbsurl = ''
    vorole = 'cmsuser'
    vogroup = ''
    publishdbsurl = ''
    tfileoutfiles = []
    edmoutfiles = []
    userdn = '/CN=Brian Bockelman'
    runs = []
    lumis = []
    dag.submitRaw(workflow, jobtype, jobsw, jobarch, inputdata, siteblacklist, sitewhitelist, 
               splitalgo, algoargs, cachefilename, cacheurl, addoutputfiles,
               userhn, userdn, savelogsflag, publishname, asyncdest, blacklistT1, dbsurl, publishdbsurl, vorole, vogroup, tfileoutfiles, edmoutfiles,
               runs, lumis, userproxy = '/tmp/x509up_u%d' % os.geteuid()) #TODO delete unused parameters


if __name__ == "__main__":
    main()
