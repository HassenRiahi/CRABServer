
"""
Create a set of files for a DAG submission.

Generates the condor submit files and the master DAG.
"""

import os
import json
import base64
import shutil
import string
import urllib
import logging
import commands
import tempfile

import TaskWorker.Actions.TaskAction as TaskAction
import TaskWorker.DataObjects.Result
import TaskWorker.WorkerExceptions

import WMCore.WMSpec.WMTask

DAG_FRAGMENT = """
JOB Job%(count)d Job.submit
#SCRIPT PRE  Job%(count)d dag_bootstrap.sh PREJOB $RETRY $JOB
#SCRIPT POST Job%(count)d dag_bootstrap.sh POSTJOB $RETURN $RETRY $MAX_RETRIES %(taskname)s %(count)d %(outputData)s %(sw)s %(asyncDest)s %(tempDest)s %(outputDest)s cmsRun_%(count)d.log.tar.gz %(remoteOutputFiles)s
#PRE_SKIP Job%(count)d 3
RETRY Job%(count)d 3 UNLESS-EXIT 2
VARS Job%(count)d count="%(count)d" runAndLumiMask="%(runAndLumiMask)s" inputFiles="%(inputFiles)s" +DESIRED_Sites="\\"%(desiredSites)s\\"" +CRAB_localOutputFiles="\\"%(localOutputFiles)s\\""

JOB ASO%(count)d ASO.submit
VARS ASO%(count)d count="%(count)d" outputFiles="%(remoteOutputFiles)s"
RETRY ASO%(count)d 3

PARENT Job%(count)d CHILD ASO%(count)d
"""

CRAB_HEADERS = \
"""
+CRAB_ReqName = %(requestname)s
+CRAB_Workflow = %(workflow)s
+CRAB_JobType = %(jobtype)s
+CRAB_JobSW = %(jobsw)s
+CRAB_JobArch = %(jobarch)s
+CRAB_InputData = %(inputdata)s
+CRAB_OutputData = %(publishname)s
+CRAB_ISB = %(cacheurl)s
+CRAB_SiteBlacklist = %(siteblacklist)s
+CRAB_SiteWhitelist = %(sitewhitelist)s
+CRAB_AdditionalOutputFiles = %(addoutputfiles)s
+CRAB_EDMOutputFiles = %(edmoutfiles)s
+CRAB_TFileOutputFiles = %(tfileoutfiles)s
+CRAB_SaveLogsFlag = %(savelogsflag)s
+CRAB_UserDN = %(userdn)s
+CRAB_UserHN = %(userhn)s
+CRAB_AsyncDest = %(asyncdest)s
+CRAB_BlacklistT1 = %(blacklistT1)s
"""

# NOTE: keep Arugments in sync with PanDAInjection.py.  ASO is very picky about argument order.
JOB_SUBMIT = CRAB_HEADERS + \
"""
CRAB_Attempt = %(attempt)d
CRAB_ISB = %(cacheurl_flatten)s
CRAB_AdditionalOutputFiles = %(addoutputfiles_flatten)s
CRAB_JobSW = %(jobsw_flatten)s
CRAB_JobArch = %(jobarch_flatten)s
CRAB_Archive = %(cachefilename_flatten)s
+CRAB_ReqName = %(requestname)s
#CRAB_ReqName = %(requestname_flatten)s
CRAB_DBSURL = %(dbsurl_flatten)s
CRAB_PublishDBSURL = %(publishdbsurl_flatten)s
CRAB_Publish = %(publication)s
CRAB_Id = $(count)
+CRAB_Id = $(count)
+CRAB_Dest = "cms://%(temp_dest)s"
+TaskType = "Job"
+MaxWallTimeMins = 1315
+AccountingGroup = %(userhn)s

+JOBGLIDEIN_CMSSite = "$$([ifThenElse(GLIDEIN_CMSSite is undefined, \\"Unknown\\", GLIDEIN_CMSSite)])"
job_ad_information_attrs = MATCH_EXP_JOBGLIDEIN_CMSSite, JOBGLIDEIN_CMSSite

universe = vanilla
Executable = gWMS-CMSRunAnalysis.sh
Output = job_out.$(CRAB_Id)
Error = job_err.$(CRAB_Id)
Log = job_log.$(CRAB_Id)
# args changed...

Arguments = "-a $(CRAB_Archive) --sourceURL=$(CRAB_ISB) --jobNumber=$(CRAB_Id) --cmsswVersion=$(CRAB_JobSW) --scramArch=$(CRAB_JobArch) '--inputFile=$(inputFiles)' '--runAndLumis=$(runAndLumiMask)' -o $(CRAB_AdditionalOutputFiles)"

transfer_input_files = CMSRunAnalysis.sh, cmscp.py
transfer_output_files = jobReport.json.$(count)
# TODO: fold this into the config file instead of hardcoding things.
Environment = SCRAM_ARCH=$(CRAB_JobArch);CRAB_TASKMANAGER_TARBALL=http://hcc-briantest.unl.edu/CMSRunAnalysis-3.3.0-pre1.tar.gz;%(additional_environment_options)s
should_transfer_files = YES
#x509userproxy = %(x509up_file)s
use_x509userproxy = true
# TODO: Uncomment this when we get out of testing mode
Requirements = (target.IS_GLIDEIN =!= TRUE) || (target.GLIDEIN_CMSSite =!= UNDEFINED)
#Requirements = ((target.IS_GLIDEIN =!= TRUE) || ((target.GLIDEIN_CMSSite =!= UNDEFINED) && (stringListIMember(target.GLIDEIN_CMSSite, DESIRED_SEs) )))
#leave_in_queue = (JobStatus == 4) && ((StageOutFinish =?= UNDEFINED) || (StageOutFinish == 0)) && (time() - EnteredCurrentStatus < 14*24*60*60)
queue
"""

ASYNC_SUBMIT = CRAB_HEADERS + \
"""
+TaskType = "ASO"
+CRAB_Id = $(count)

universe = local
Executable = dag_bootstrap.sh
Arguments = "ASO %(asyncdest_flatten)s %(temp_dest)s %(output_dest)s $(count) $(Cluster).$(Process) cmsRun_$(count).log.tar.gz $(outputFiles)"
Output = aso.$(count).out
transfer_input_files = job_log.$(count), jobReport.json.$(count)
+TransferOutput = ""
Error = aso.$(count).err
Environment = PATH=/usr/bin:/bin;CRAB3_VERSION=3.3.0-pre1;%(additional_environment_options)s
use_x509userproxy = true
#x509userproxy = %(x509up_file)s
#leave_in_queue = (JobStatus == 4) && ((StageOutFinish =?= UNDEFINED) || (StageOutFinish == 0)) && (time() - EnteredCurrentStatus < 14*24*60*60)
queue
"""

SPLIT_ARG_MAP = { "LumiBased" : "lumis_per_job",
                  "FileBased" : "files_per_job",}

LOGGER = None

def transform_strings(input):
    """
    Converts the arguments in the input dictionary to the arguments necessary
    for the job submit file string.
    """
    info = {}
    for var in 'workflow', 'jobtype', 'jobsw', 'jobarch', 'inputdata', 'splitalgo', 'algoargs', \
           'cachefilename', 'cacheurl', 'userhn', 'publishname', 'asyncdest', 'dbsurl', 'publishdbsurl', \
           'userdn', 'requestname', 'publication':
        val = input.get(var, None)
        if val == None:
            info[var] = 'undefined'
        else:
            info[var] = json.dumps(val)

    for var in 'savelogsflag', 'blacklistT1':
        info[var] = int(input[var])

    for var in 'siteblacklist', 'sitewhitelist', 'addoutputfiles', \
           'tfileoutfiles', 'edmoutfiles':
        val = input[var]
        if val == None:
            info[var] = "{}"
        else:
            info[var] = "{" + json.dumps(val)[1:-1] + "}"

    #TODO: We don't handle user-specified lumi masks correctly.
    info['lumimask'] = '"' + json.dumps(WMCore.WMSpec.WMTask.buildLumiMask(input['runs'], input['lumis'])).replace(r'"', r'\"') + '"'
    splitArgName = SPLIT_ARG_MAP[input['splitalgo']]
    info['algoargs'] = '"' + json.dumps({'halt_job_on_file_boundaries': False, 'splitOnRun': False, splitArgName : input['algoargs']}).replace('"', r'\"') + '"'
    info['attempt'] = 0

    for var in ["cacheurl", "jobsw", "jobarch", "cachefilename", "asyncdest", "dbsurl", "publishdbsurl", "requestname"]:
        info[var+"_flatten"] = input[var]

    # TODO: PanDA wrapper wants some sort of dictionary.
    info["addoutputfiles_flatten"] = '{}'

    info["output_dest"] = os.path.join("/store/user", input['userhn'], input['workflow'], input['publishname'])
    info["temp_dest"] = os.path.join("/store/temp/user", input['userhn'], input['workflow'], input['publishname'])
    info['x509up_file'] = os.path.split(input['user_proxy'])[-1]
    info['user_proxy'] = input['user_proxy']
    info['scratch'] = input['scratch']

    return info

# TODO: DagmanCreator started life as a flat module, then the DagmanCreator class
# was later added.  We need to come back and make the below methods class methods

def makeJobSubmit(task):
    if os.path.exists("Job.submit"):
        return
    # From here on out, we convert from tm_* names to the DataWorkflow names
    info = dict(task)
    info['workflow'] = task['tm_taskname'].split("_")[-1]
    info['jobtype'] = 'analysis'
    info['jobsw'] = info['tm_job_sw']
    info['jobarch'] = info['tm_job_arch']
    info['inputdata'] = info['tm_input_dataset']
    info['splitalgo'] = info['tm_split_algo']
    info['algoargs'] = info['tm_split_args']
    info['cachefilename'] = info['tm_user_sandbox']
    info['cacheurl'] = info['tm_cache_url']
    info['userhn'] = info['tm_username']
    info['publishname'] = info['tm_publish_name']
    info['asyncdest'] = info['tm_asyncdest']
    info['dbsurl'] = info['tm_dbs_url']
    info['publishdbsurl'] = info['tm_publish_dbs_url']
    info['publication'] = info['tm_publication']
    info['userdn'] = info['tm_user_dn']
    info['requestname'] = string.replace(task['tm_taskname'],'"', '')
    info['savelogsflag'] = 0
    info['blacklistT1'] = 0
    info['siteblacklist'] = task['tm_site_blacklist']
    info['sitewhitelist'] = task['tm_site_whitelist']
    info['addoutputfiles'] = task['tm_outfiles']
    info['tfileoutfiles'] = task['tm_tfile_outfiles']
    info['edmoutfiles'] = task['tm_edm_outfiles']
    # TODO: pass through these correctly.
    info['runs'] = []
    info['lumis'] = []
    info = transform_strings(info)
    info.setdefault("additional_environment_options", '')
    print info
    print "There was the info ****"
    logging.info("There was the info ***")
    with open("Job.submit", "w") as fd:
        fd.write(JOB_SUBMIT % info)
    with open("ASO.submit", "w") as fd:
        fd.write(ASYNC_SUBMIT % info)
        
    return info

def make_specs(task, jobgroup, availablesites, outfiles, startjobid):
    specs = []
    i = startjobid
    for job in jobgroup.getJobs():
        inputFiles = json.dumps([inputfile['lfn'] for inputfile in job['input_files']]).replace('"', r'\"\"')
        runAndLumiMask = json.dumps(job['mask']['runAndLumis']).replace('"', r'\"\"')
        desiredSites = ", ".join(availablesites)
        i += 1
        remoteOutputFiles = []
        localOutputFiles = []
        for origFile in outfiles:
            info = origFile.rsplit(".", 1)
            if len(info) == 2:
                fileName = "%s_%d.%s" % (info[0], i, info[1])
            else:
                fileName = "%s_%d" % (origFile, i)
            remoteOutputFiles.append("%s" % fileName)
            localOutputFiles.append("%s?remoteName=%s" % (origFile, fileName))
        remoteOutputFiles = " ".join(remoteOutputFiles)
        localOutputFiles = ", ".join(localOutputFiles)
        specs.append({'count': i, 'runAndLumiMask': runAndLumiMask, 'inputFiles': inputFiles,
                      'desiredSites': desiredSites, 'remoteOutputFiles': remoteOutputFiles,
                      'localOutputFiles': localOutputFiles, 'asyncDest': task['tm_asyncdest'],
                      'sw': task['tm_job_sw'], 'taskname': task['tm_taskname'],
                      'outputData': task['tm_publish_name'],
                      'tempDest': os.path.join("/store/temp/user", task['tm_username'], task['tm_taskname'], task['tm_publish_name']),
                      'outputDest': os.path.join("/store/user", task['tm_username'], task['tm_taskname'], task['tm_publish_name']),})

        LOGGER.debug(specs[-1])
    return specs, i

def create_subdag(splitter_result, **kwargs):

    global LOGGER
    if not LOGGER:
        LOGGER = logging.getLogger("DagmanCreator")

    startjobid = 0
    specs = []

    info = makeJobSubmit(kwargs['task'])

    outfiles = kwargs['task']['tm_outfiles'] + kwargs['task']['tm_tfile_outfiles'] + kwargs['task']['tm_edm_outfiles']

    os.chmod("CMSRunAnalysis.sh", 0755)

    server_data = []

    #fixedsites = set(self.config.Sites.available)
    for jobgroup in splitter_result:
        jobs = jobgroup.getJobs()

        if not jobs:
            possiblesites = []
        else:
            possiblesites = jobs[0]['input_files'][0]['locations']
        LOGGER.debug("Possible sites: %s" % possiblesites)
        LOGGER.debug('Blacklist: %s; whitelist %s' % (kwargs['task']['tm_site_blacklist'], kwargs['task']['tm_site_whitelist']))
        if kwargs['task']['tm_site_whitelist']:
            availablesites = set(kwargs['task']['tm_site_whitelist'])
        else:
            availablesites = set(possiblesites) - set(kwargs['task']['tm_site_blacklist'])
        #availablesites = set(availablesites) & fixedsites
        availablesites = [str(i) for i in availablesites]
        LOGGER.info("Resulting available sites: %s" % ", ".join(availablesites))

        if not availablesites:
            msg = "No site available for submission of task %s" % (kwargs['task']['tm_taskname'])
            raise TaskWorker.WorkerExceptions.NoAvailableSite(msg)

        jobgroupspecs, startjobid = make_specs(kwargs['task'], jobgroup, availablesites, outfiles, startjobid)
        specs += jobgroupspecs

        # TODO: PanDA implementation makes a POST call about job data ... not sure what our equiv is.

    dag = ""
    for spec in specs:
        dag += DAG_FRAGMENT % spec

    with open("RunJobs.dag", "w") as fd:
        fd.write(dag)

    task_name = kwargs['task'].get('CRAB_ReqName', kwargs['task'].get('tm_taskname', ''))
    userdn = kwargs['task'].get('CRAB_UserDN', kwargs['task'].get('tm_user_dn', ''))

    # When running in standalone mode, we want to record the number of jobs in the task
    if ('CRAB_ReqName' in kwargs['task']) and ('CRAB_UserDN' in kwargs['task']):
        const = 'TaskType =?= \"ROOT\" && CRAB_ReqName =?= "%s" && CRAB_UserDN =?= "%s"' % (task_name, userdn)
        cmd = "condor_qedit -const '%s' CRAB_JobCount %d" % (const, len(jobgroup.getJobs()))
        LOGGER.debug("+ %s" % cmd)
        status, output = commands.getstatusoutput(cmd)
        if status:
            LOGGER.error(output)
            LOGGER.error("Failed to record the number of jobs.")
            return 1

    return info


def getLocation(default_name, checkout_location):
    loc = default_name
    if not os.path.exists(loc):
        if 'CRAB3_CHECKOUT' not in os.environ:
            raise Exception("Unable to locate %s" % loc)
        loc = os.path.join(os.environ['CRAB3_CHECKOUT'], checkout_location, loc)
    loc = os.path.abspath(loc)
    return loc

class DagmanCreator(TaskAction.TaskAction):
    """
    Given a task definition, create the corresponding DAG files for submission
    into HTCondor
    """

    def executeInternal(self, *args, **kw):
        global LOGGER
        LOGGER = self.logger

        cwd = None
        if hasattr(self.config, 'TaskWorker') and hasattr(self.config.TaskWorker, 'scratchDir'):
            temp_dir = tempfile.mkdtemp(prefix='_' + kw['task']['tm_taskname'], dir=self.config.TaskWorker.scratchDir)

            # FIXME: In PanDA, we provided the executable as a URL.
            # So, the filename becomes http:// -- and doesn't really work.  Hardcoding the analysis wrapper.
            #transform_location = getLocation(kw['task']['tm_transformation'], 'CAFUtilities/src/python/transformation/CMSRunAnalysis/')
            transform_location = getLocation('CMSRunAnalysis.sh', 'CAFTaskWorker/scripts/')
            cmscp_location = getLocation('cmscp.py', 'CAFTaskWorker/scripts/')
            gwms_location = getLocation('gWMS-CMSRunAnalysis.sh', 'CAFTaskWorker/scripts/')
            dag_bootstrap_location = getLocation('dag_bootstrap_startup.sh', 'CAFTaskWorker/scripts/')
            bootstrap_location = getLocation("dag_bootstrap.sh", "CAFTaskWorker/scripts/")

            cwd = os.getcwd()
            os.chdir(temp_dir)
            shutil.copy(transform_location, '.')
            shutil.copy(cmscp_location, '.')
            shutil.copy(gwms_location, '.')
            shutil.copy(dag_bootstrap_location, '.')
            shutil.copy(bootstrap_location, '.')

            kw['task']['scratch'] = temp_dir

        try:
            info = create_subdag(*args, **kw)
        finally:
            if cwd:
                os.chdir(cwd)
        return TaskWorker.DataObjects.Result.Result(task=kw['task'], result=(temp_dir, info))

    def execute(self, *args, **kw):
        try:
            return self.executeInternal(*args, **kw)
        except Exception, e:
            configreq = {'workflow': kw['task']['tm_taskname'],
                             'substatus': "FAILED",
                             'subjobdef': -1,
                             'subuser': kw['task']['tm_user_dn'],
                             'subfailure': base64.b64encode(str(e)),}
            self.logger.error("Pushing information centrally %s" %(str(configreq)))
            data = urllib.urlencode(configreq)
            self.server.put(self.resturl, data=data)
            raise

