"""Stable Service blueprints; workload templates live in deployment/rke2/roles."""


def headless(name, app):
    return {'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':'traffic-mlops'},
            'spec':{'clusterIP':'None','publishNotReadyAddresses':False,'selector':{'app':app},
                    'ports':[{'name':'http','port':8000,'targetPort':8000}]}}


def preview():
    return {'apiVersion':'v1','kind':'Service','metadata':{'name':'api-preview','namespace':'traffic-mlops'},
            'spec':{'selector':{'app':'api'},'ports':[{'name':'http','port':8000,'targetPort':8000}]}}
