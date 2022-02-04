import os
import subprocess
import threading
import time
from . import worker
from abc import ABC, abstractmethod, ABCMeta
import logging as l
import functools
import requests
import json
import re

class NLPAnnotator(worker.Worker):
    __metaclass__ = ABCMeta  
    def __init__(self, name, orchestrator_ref, settings): 
        super().__init__(name = name, orchestrator_ref = orchestrator_ref, settings = settings)    
        self._models_path = settings["pandem"]["source"]["nlp"]["models_path"]
        self._tf_port = settings["pandem"]["source"]["nlp"]["tensorflow_server_port"]
        self._tf_url = f"{settings['pandem']['source']['nlp']['tensorflow_server_protocol']}://{settings['pandem']['source']['nlp']['tensorflow_server_host']}:{settings['pandem']['source']['nlp']['tensorflow_server_port']}"
        self._tf_version = settings["pandem"]["source"]['nlp']["tensorflow_server_version"]
        self._model_categories = settings["pandem"]["source"]["nlp"]["categories"]
        self._model_languages = settings["pandem"]["source"]["nlp"]["languages"]

    def on_start(self):
        super().on_start()
        self._storage_proxy = self._orchestrator_proxy.get_actor('storage').get().proxy()
        self._pipeline_proxy = self._orchestrator_proxy.get_actor('pipeline').get().proxy() 
        self._variables_proxy = self._orchestrator_proxy.get_actor('variables').get().proxy()
        #self._models = self.get_models()
        self._models = None

    def annotate(self, list_of_tuples, path, job):

      if self._models is None:
          self._models = self.get_models()


      self.launch_server_if_needed()

      # gathering information about nlp categories
      endpoints = self.model_endpoints()
      categories = {m:self._model_categories[m] for m in endpoints.keys() if (m in self._model_categories) }
      
      #gatherint information for geo annotation
      variables = self._variables_proxy.get_variables().get()
      geos = {var["variable"] for var in variables.values() if var["type"] == "geo_referential"}
      alias_vars = {
        var["variable"]:var["linked_attributes"][0] 
        for var in variables.values() 
        if var["type"] == "referential_alias" and var["linked_attributes"] is not None and var["linked_attributes"][0] in geos
      }
      aliases = {}
      for alias_var, code_var in alias_vars.items():
        alias_values = self._variables_proxy.read_variable(alias_var, {}).get()
        if alias_values is not None:
          alias_map = {t["attr"][alias_var].lower():t["attrs"][code_var] for t in alias_values if "attr" in t and "attrs" in t and alias_var in t["attr"] and code_var in t["attrs"]} 
          if code_var not in aliases:
            aliases[code_var] = alias_map
          else :
            aliases[code_var].update(alias_map)
      
      alias_regex = {code_var:re.compile('|'.join([f"\\b{re.escape(alias)}\\b" for alias in aliases[code_var]])) for code_var in aliases}

      text_field = "article_text"
      lang_field = "article_language"

      for lang in self._model_languages:
        to_annotate = [ t for t in list_of_tuples['tuples'] if "attrs" in t and text_field in t["attrs"] and lang_field in t["attrs"] and t["attrs"][lang_field]==lang ]
        if len(to_annotate) > 0:
          # Annotating using tensorflow categories
          for m in categories.keys():
            if m in self._model_languages[lang]: 
              texts = [t["attrs"][text_field] for t in to_annotate]
              data = json.dumps({"instances": [[t] for t in texts]})
              result = requests.post(f"{endpoints[m]}:predict", data = data, headers = {'content-type': "application/json"}).content
              annotations = json.loads(result)["predictions"]
              for t, pred in zip(to_annotate, annotations):
                best = functools.reduce(lambda a, b: a if a[1]>b[1] else b, enumerate(pred))[0]
                t["attrs"][f"article_cat_{m}"] = categories[m][best]
      
      # Annotating geographically using extra simplistic approach
      to_annotate = [ t for t in list_of_tuples['tuples'] if "attrs" in t and text_field in t["attrs"]]
      for geo_var, regex in alias_regex.items():
        texts = [t["attrs"][text_field] for t in to_annotate]
        for t in to_annotate:
          if not geo_var in t["attrs"]:
            text = t["attrs"][text_field].lower()
            match = re.search(alias_regex[geo_var], text)
            if match is not None:
              matched_alias = match.group()
              t["attrs"][geo_var] = aliases[geo_var][matched_alias]


      
      self._pipeline_proxy.annotate_end(list_of_tuples, path = path, job = job)

    def launch_server_if_needed(self):
        # ensuring models are running and launching them as docker command if not
        if not self.models_up():
          self.check_docker_installed()
          threading.Thread(target=self.run_model_server).start()
        while not self.models_up():
          l.info("Waiting 1 until models are running")
          time.sleep(1)
        l.debug("models running and ready to serve!!")

    def check_docker_installed(self):
      if subprocess.getstatusoutput('docker')[0] != 0:
        raise ValueError("We cannot find docker in order to run the SMA componenents please launche them manually")

    def get_models(self):
      if os.path.exists(self._models_path):
        return list(filter(lambda v: not v.startswith("."), next(os.walk(self._models_path))[1]))
      else: 
        raise FileNotFoundError(f"Cannot find the NLP models folder {self._models_path}")

    def model_endpoints(self):
      return {m:f"{self._tf_url}/v1/models/{m}" for m in self.get_models()}


    def models_up(self):
      for ep in self.model_endpoints().values():
        try:
          if requests.get(ep).status_code != 200:
            return False
        except Exception as err:
          return False
      return True

    def run_model_server(self):
      #sudo_password = getpass.getpass(prompt='enter your sudo password: ')
      #print("pwd received!")
      cmd = self.docker_launch_command()
      l.debug(f"command to send: {cmd}")
      #p = subprocess.Popen(self.launch_docker_command(), stderr=subprocess.PIPE, stdout=subprocess.PIPE,  stdin=subprocess.PIPE)
      p = subprocess.run(self.docker_launch_command())
      
      #try:
      #  out, err = p.communicate(input=(sudo_password+'\n').encode(),timeout=20)
      #except subprocess.TimeoutExpired:
      #  print("\n\n\n\n\n\n BUAAAAAAAAAAAAAAAAAAAAAA")
      #  p.kill()

    def docker_launch_command(self):
      if os.name == "posix":
        cmd = ["sudo"]
      else: 
        cmd = []
      cmd.extend(["docker", "run", "-p", f"{self._tf_port}:8501", "-it"]) 
      
      for model in self._models:
        cmd.extend(["-v",  f"{self._models_path}{os.sep}{model}:/models/{model}/1/"]) 

      cmd.extend(["-v", f"{self._models_path}{os.sep}models.config:/models/models.config"])
      cmd.extend(["--rm", "-t", f"tensorflow/serving:{self._tf_version}", "--model_config_file=/models/models.config"])
      return cmd
       

