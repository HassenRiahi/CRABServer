
"""
Submit a DAG directory created by the DagmanCreator component.
"""

import os
import base64
import random
import urllib
import traceback

import HTCondorUtils
import HTCondorLocator

import TaskWorker.Actions.TaskAction as TaskAction
import TaskWorker.DataObjects.Result as Result

from TaskWorker.Actions.DagmanCreator import CRAB_HEADERS

# Bootstrap either the native module or the BossAir variant.
try:
    import classad
    import htcondor
except ImportError, _:
    #pylint: disable=C0103
    classad = None
    htcondor = None

CRAB_META_HEADERS = \
"""
+CRAB_SplitAlgo = %(splitalgo)s
+CRAB_AlgoArgs = %(algoargs)s
+CRAB_ConfigDoc = %(configdoc)s
+CRAB_PublishName = %(publishname)s
+CRAB_DBSUrl = %(dbsurl)s
+CRAB_PublishDBSUrl = %(publishdbsurl)s
+CRAB_LumiMask = %(lumimask)s
"""

# NOTE: Changes here must be synchronized with the submitDirect function below
MASTER_DAG_SUBMIT_FILE = CRAB_HEADERS + CRAB_META_HEADERS + \
"""
+CRAB_Attempt = 0
+CRAB_Workflow = %(workflow)s
+CRAB_UserDN = %(userdn)s
universe = local
# Can't ever remember if this is quotes or not
+CRAB_ReqName = "%(requestname)s"
scratch = %(scratch)s
bindir = %(bindir)s
output = $(scratch)/request.out
error = $(scratch)/request.err
executable = $(bindir)/dag_bootstrap_startup.sh
arguments = $(bindir)/master_dag
transfer_input_files = %(inputFilesString)s
transfer_output_files = %(outputFilesString)s
leave_in_queue = (JobStatus == 4) && ((StageOutFinish =?= UNDEFINED) || (StageOutFinish == 0)) && (time() - EnteredCurrentStatus < 14*24*60*60)
on_exit_remove = ( ExitSignal =?= 11 || (ExitCode =!= UNDEFINED && ExitCode >=0 && ExitCode <= 2))
+OtherJobRemoveRequirements = DAGManJobId =?= ClusterId
remove_kill_sig = SIGUSR1
+HoldKillSig = "SIGUSR1"
on_exit_hold = (ExitCode =!= UNDEFINED && ExitCode != 0)
+Environment= strcat("PATH=/usr/bin:/bin:/opt/glidecondor/bin CONDOR_ID=", ClusterId, ".", ProcId, " %(additional_environment_options)s")
+RemoteCondorSetup = "%(remote_condor_setup)s"
+TaskType = "ROOT"
X509UserProxy = %(user_proxy)s
queue 1
"""

SUBMIT_INFO = [ \
            ('CRAB_Workflow', 'workflow'),
            ('CRAB_ReqName', 'requestname'),
            ('CRAB_JobType', 'jobtype'),
            ('CRAB_JobSW', 'jobsw'),
            ('CRAB_JobArch', 'jobarch'),
            ('CRAB_InputData', 'inputdata'),
            ('CRAB_ISB', 'cacheurl'),
            ('CRAB_SiteBlacklist', 'siteblacklist'),
            ('CRAB_SiteWhitelist', 'sitewhitelist'),
            ('CRAB_AdditionalOutputFiles', 'addoutputfiles'),
            ('CRAB_EDMOutputFiles', 'edmoutfiles'),
            ('CRAB_TFileOutputFiles', 'tfileoutfiles'),
            ('CRAB_SaveLogsFlag', 'savelogsflag'),
            ('CRAB_UserDN', 'userdn'),
            ('CRAB_UserHN', 'userhn'),
            ('CRAB_AsyncDest', 'asyncdest'),
            ('CRAB_BlacklistT1', 'blacklistT1'),
            ('CRAB_SplitAlgo', 'splitalgo'),
            ('CRAB_AlgoArgs', 'algoargs'),
            ('CRAB_PublishName', 'publishname'),
            ('CRAB_DBSUrl', 'dbsurl'),
            ('CRAB_PublishDBSUrl', 'publishdbsurl'),
            ('CRAB_LumiMask', 'lumimask')]

def addCRABInfoToClassAd(ad, info):
    """
    Given a submit ClassAd, add in the appropriate CRAB_* attributes
    from the info directory
    """
    for adName, dictName in SUBMIT_INFO:
        ad[adName] = classad.ExprTree(str(info[dictName]))

class DagmanSubmitter(TaskAction.TaskAction):

    """
    Submit a DAG to a HTCondor schedd
    """

    def execute(self, *args, **kw):
        try:
            return self.executeInternal(*args, **kw)
        except Exception, e:
            msg = "Failed to submit task %s; '%s'" % (kw['task']['tm_taskname'], str(e))
            self.logger.error(msg)
            configreq = {'workflow': kw['task']['tm_taskname'],
                         'status': "FAILED",
                         'subresource': 'failure',
                         'failure': base64.b64encode(msg)}
            self.server.post(self.resturl, data = urllib.urlencode(configreq))
            raise

    def executeInternal(self, *args, **kw):

        if not htcondor:
            raise Exception("Unable to import HTCondor module")

        task = kw['task']
        tempDir = args[0][0]
        info = args[0][1]

        cwd = os.getcwd()
        os.chdir(tempDir)

        #FIXME: hardcoding the transform name for now.
        #inputFiles = ['gWMS-CMSRunAnalysis.sh', task['tm_transformation'], 'cmscp.py', 'RunJobs.dag']
        inputFiles = ['gWMS-CMSRunAnalysis.sh', 'CMSRunAnalysis.sh', 'cmscp.py', 'RunJobs.dag', 'Job.submit', 'ASO.submit', 'dag_bootstrap.sh']
        info['inputFilesString'] = ", ".join(inputFiles)
        outputFiles = ["RunJobs.dag.dagman.out", "RunJobs.dag.rescue.001"]
        info['outputFilesString'] = ", ".join(outputFiles)
        arg = "RunJobs.dag"

        try:
            info['remote_condor_setup'] = ''
            loc = HTCondorLocator.HTCondorLocator(self.config)
            scheddName = loc.getSchedd()
            self.logger.debug("Using scheduler %s." % scheddName)
            schedd, address = loc.getScheddObj(scheddName)
            if address:
                self.submitDirect(schedd, 'dag_bootstrap_startup.sh', arg, info)
            else:
                jdl = MASTER_DAG_SUBMIT_FILE % info
                schedd.submitRaw(task['tm_taskname'], jdl, task['user_proxy'], inputFiles)
        finally:
            os.chdir(cwd)

        configreq = {'workflow': kw['task']['tm_taskname'],
                     'status': "SUBMITTED",
                     'jobset': "-1",
                     'subresource': 'success',}
        self.logger.debug("Pushing information centrally %s" %(str(configreq)))
        data = urllib.urlencode(configreq)
        self.server.post(self.resturl, data = data)
    
        return Result.Result(task=kw['task'], result=(-1))

    def submitDirect(self, schedd, cmd, arg, info): #pylint: disable=R0201
        """
        Submit directly to the schedd using the HTCondor module
        """
        dagAd = classad.ClassAd()
        addCRABInfoToClassAd(dagAd, info)

        # NOTE: Changes here must be synchronized with the job_submit in DagmanCreator.py in CAFTaskWorker
        dagAd["Out"] = str(os.path.join(info['scratch'], "request.out"))
        dagAd["Err"] = str(os.path.join(info['scratch'], "request.err"))
        dagAd["CRAB_Attempt"] = 0
        dagAd["JobUniverse"] = 12
        dagAd["HoldKillSig"] = "SIGUSR1"
        dagAd["Cmd"] = cmd
        dagAd['Args'] = arg
        dagAd["TransferInput"] = str(info['inputFilesString'])
        dagAd["LeaveJobInQueue"] = classad.ExprTree("(JobStatus == 4) && ((StageOutFinish =?= UNDEFINED) || (StageOutFinish == 0))")
        dagAd["TransferOutput"] = info['outputFilesString']
        dagAd["OnExitRemove"] = classad.ExprTree("( ExitSignal =?= 11 || (ExitCode =!= UNDEFINED && ExitCode >=0 && ExitCode <= 2))")
        dagAd["OtherJobRemoveRequirements"] = classad.ExprTree("DAGManJobId =?= ClusterId")
        dagAd["RemoveKillSig"] = "SIGUSR1"
        dagAd["Environment"] = classad.ExprTree('strcat("PATH=/usr/bin:/bin CONDOR_ID=", ClusterId, ".", ProcId," %s")' % info['additional_environment_options'])
        dagAd["RemoteCondorSetup"] = info['remote_condor_setup']
        dagAd["Requirements"] = classad.ExprTree('true || false')
        dagAd["TaskType"] = "ROOT"
        dagAd["X509UserProxy"] = info['user_proxy']

        with HTCondorUtils.AuthenticatedSubprocess(info['user_proxy']) as (parent, rpipe):
            if not parent:
                resultAds = []
                schedd.submit(dagAd, 1, True, resultAds)
                schedd.spool(resultAds)
        results = rpipe.read()
        if results != "OK":
            raise Exception("Failure when submitting HTCondor task: '%s'" % results)

        schedd.reschedule()

