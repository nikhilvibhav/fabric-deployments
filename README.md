## Camel deployment

Deploy JAR-based Camel services with the [Python Fabric/Invoke CLI](deployment/README.md).
The repository also contains Ansible playbooks for starting and stopping services
in Preprod.

## Starting and stopping camel services in Preprod

### Usage

We find that after a number of PVT runs Camel services in PPD tend to get stopped, most likely this is due to the OS killing the processes as the RAM used on the server is near it's limit.

We added ansible code to monitor and restart services when needed

### Files structure

```
├── camel_services_start.yaml
├── camel_services_stop.yaml
└── environments
    └── preprod
        ├── host_vars
        │   ├── rmg52app001ppd.yaml
        │   ├── rmg52app002ppd.yaml
        │   ├── rmg52app003ppd.yaml
        │   ├── rmg52app004ppd.yaml
        │   ├── rmg52app007ppd.yaml
        │   ├── rmg52app008ppd.yaml
        │   └── rmg52app009ppd.yaml
        └── inventory
```

- environments/preprod/inventory: contains a list of all camel services hosts in Preprod environment.

- environments/preprod/host_vars: each host var file contains a list of all the services that should be running on that host, as well as those that shouldn't (optional).

- camel_services_start.yaml: used to start all the services in each host.

- camel_services_stop.yaml:  used to stop all the services in each host.