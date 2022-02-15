import subprocess
import os
import time
import threading
from . import worker
from . import util
from datetime import datetime, timedelta
import copy
import logging as l


class Standardizer(worker.Worker):
    def __init__(self, name, orchestrator_ref, settings): 
        super().__init__(name = name, orchestrator_ref = orchestrator_ref, settings = settings)

    def on_start(self):
        super().on_start()
        self._storage_proxy=self._orchestrator_proxy.get_actor('storage').get().proxy()
        self._variables_proxy=self._orchestrator_proxy.get_actor('variables').get().proxy()
        self._pipeline_proxy=self._orchestrator_proxy.get_actor('pipeline').get().proxy()

    #def standardize(self, tuples_to_validate, job):
    def standardize(self, tuples, path, job, dls):  
        """
            IN:         tuples_to_validate and object job
            ACTIONS:    check the code and updates with a code if the values are in a ref
            OUT:        if issues not null list of issue
                        else list of tuples standardize
        """
        std_tuples={'scope':{}, 'tuples':[]}
        std_var={}
        variables=self._variables_proxy.get_variables().get()
        list_issues=[]
        list_ref=[]
        global_tuple={}
        update_tuple={}
        refs_alias={}
        refs_values={}
        ignore_check = set(c["variable"] for c in dls["columns"] if "action" in c and c["action"] == "insert")
        type_translate=['referential_alias']
        type_validate=['referential', 'geo_referential', 'referential_alias']
        for i in range(-2, len(tuples['tuples'])):
            std_var = {}
            #retrieves the globals variable
            if i == -2: 
                std_var['attrs']=dict([(x['variable'],x['value']) for x in tuples['scope']['globals'] if 'value' in x])
            #retrieves the update variable
            elif i == -1: 
                std_var['attrs']=dict([(x['variable'],x['value']) for x in tuples['scope']['update_scope']])
            #retrieves the tupple
            else:
                std_var=copy.deepcopy(tuples['tuples'][i])
            for var_name in std_var['attrs'].copy().keys():
                #retrieves the referentiel
                if var_name not in refs_values and var_name in variables and variables[var_name]['type'] in type_validate:
                    referential=self._variables_proxy.get_referential(var_name).get()
                    if referential is not None:
                        refs_values[var_name]=set([x['attr'][var_name] for x in referential])
                    elif var_name not in ignore_check : 
                        self.delay_standardize(tuples = tuples, path = path, job = job, dls = dls, source_name = tuples['scope']['source'], var_name = var_name)
                        return
                if var_name not in refs_alias and var_name in variables and variables[var_name]['type'] in type_translate: 
                    alias=self._variables_proxy.get_referential(var_name).get() 
                    if alias is not None:
                        code=variables[var_name]['linked_attributes'][0]
                        refs_alias[var_name] = dict((x['attr'][var_name],x['attrs'][code]) for x in alias)
                    elif var_name not in ignore_check : 
                        self.delay_standardize(tuples = tuples, path = path, job = job, dls = dls, source_name = tuples['scope']['source'], var_name = var_name)
                        return

                var_type = variables[var_name]['type']
                #variable type is referentiel_translate
                if type(std_var['attrs'][var_name]) == list:
                  values = std_var['attrs'][var_name]
                  islist = True
                else: 
                  values = [std_var['attrs'][var_name]] 
                  islist = False
                if var_name in ignore_check or var_name not in variables:
                  pass
                #elif variables[var_name]['type'] in (type_validate + type_translate) and refs_values[var_name] is None:
                elif var_type in type_translate + type_validate :
                  new_values = []
                  var_ref = refs_values[var_name]
                  if var_type in type_translate:
                    std_var['attrs'].pop(var_name)
                  for var_value in values:
                    if  var_value is not None and var_value in var_ref:
                      if var_type in type_translate:
                        new_values.append(refs_alias[var_name][var_value])
                      else: 
                        new_values.append(var_value)
                    elif var_value is not None:
                      #Create a issue since validation failed 
                      file_name = tuples['scope']['file_name']
                      if i >= 0:
                        line_number = tuples['tuples'][i]['attrs']['line_number']
                      else:
                        line_number = -1
                      message=(f"Code {var_value} does not exist in referential '{var_name}'. Line {line_number} in file {file_name} (source: '{tuples['scope']['source']}').")
                      issue={ "step":job['step'], 
                              "line":line_number, 
                              "source":tuples['scope']['source'], 
                              "file":file_name, 
                              "message":message, 
                              "raised_on":datetime.now(), 
                              "job_id":job['id'], 
                              "issue_type":"ref-not-found",
                              'issue_severity':"warning"
                      }
                      list_issues.append(issue)
                  if len(new_values)>0:
                    if var_type in type_translate:
                      dest_var = code
                    else:
                      dest_var = var_name
                    if islist:
                      std_var['attrs'][dest_var]=new_values
                    else:
                      std_var['attrs'][dest_var]=new_values[0]
            if i == -2: 
                global_tuple=std_var
            elif i == -1: 
                update_tuple=std_var
            else:
                for cle, value in global_tuple['attrs'].items():
                    if cle not in std_var['attrs']:
                       std_var['attrs'][cle]=value
                std_tuples['tuples'].append(std_var)
        std_tuples['scope']['update_scope']= [*({'variable':k, 'value':v} for k,v in update_tuple['attrs'].items())]
        #print("\n".join(util.pretty(std_tuples).split("\n")[0:100]))
        self._pipeline_proxy.standardize_end(tuples = std_tuples, issues = list_issues, path = path, job = job)

    def delay_standardize(self, tuples, path, job, dls, var_name, source_name):
        # Delaying standardisation for one minute
        l.info(f"1 minute delay on standardisation for job {job['id']} for source {source_name} since variable {var_name} has not yet been published")
        self.register_action(
          repeat = worker.Repeat(timedelta(minutes = 1), last_exec = datetime.now()),  
          action = lambda: self._self_proxy.standardize(tuples = tuples, path = path, job = job, dls = dls),
          oneshot = True
        )
