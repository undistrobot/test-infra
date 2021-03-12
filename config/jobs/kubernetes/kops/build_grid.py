# Copyright 2020 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import zlib
import yaml

import boto3 # pylint: disable=import-error

kubetest2_template = """
- name: {{job_name}}
  cron: '{{cron}}'
  labels:
    preset-service-account: "true"
    preset-aws-ssh: "true"
    preset-aws-credential: "true"
  decorate: true
  decoration_config:
    timeout: {{job_timeout}}
  extra_refs:
  - org: kubernetes
    repo: kops
    base_ref: master
    workdir: true
    path_alias: k8s.io/kops
  spec:
    containers:
    - command:
      - runner.sh
      args:
      - bash
      - -c
      - |
        make test-e2e-install
        kubetest2 kops \\
          -v 2 \\
          --up --down \\
          --cloud-provider=aws \\
          --create-args="{{create_args}}" \\
          --env=KOPS_FEATURE_FLAGS={{kops_feature_flags}} \\
          --kops-version-marker={{kops_deploy_url}} \\
          --kubernetes-version={{k8s_deploy_url}} \\
          --terraform-version={{terraform_version}} \\
          --test=kops \\
          -- \\
          --ginkgo-args="--debug" \\
          --test-args="-test.timeout={{test_timeout}} -num-nodes=0" \\
          --test-package-bucket={{test_package_bucket}} \\
          --test-package-dir={{test_package_dir}} \\
          --test-package-marker={{marker}} \\
          --parallel={{test_parallelism}} \\
          --focus-regex="{{focus_regex}}" \\
          --skip-regex="{{skip_regex}}"
      env:
      - name: KUBE_SSH_KEY_PATH
        value: /etc/aws-ssh/aws-ssh-private
      - name: KUBE_SSH_USER
        value: {{kops_ssh_user}}
      image: {{e2e_image}}
      imagePullPolicy: Always
      resources:
        limits:
          memory: 3Gi
        requests:
          cpu: "2"
          memory: 3Gi
"""

# We support rapid focus on a few tests of high concern
# This should be used for temporary tests we are evaluating,
# and ideally linked to a bug, and removed once the bug is fixed
run_hourly = [
]

run_daily = [
    'kops-grid-scenario-public-jwks',
    'kops-grid-scenario-arm64',
    'kops-grid-scenario-aws-cloud-controller-manager',
    'kops-grid-scenario-serial-test-for-timeout',
    'kops-grid-scenario-terraform',
]

# These are job tab names of unsupported grid combinations
skip_jobs = [
]

def simple_hash(s):
    # & 0xffffffff avoids python2/python3 compatibility
    return zlib.crc32(s.encode()) & 0xffffffff

def build_cron(key, runs_per_day):
    runs_per_week = 0
    minute = simple_hash("minutes:" + key) % 60
    hour = simple_hash("hours:" + key) % 24
    day_of_week = simple_hash("day_of_week:" + key) % 7

    if runs_per_day > 0:
        hour_denominator = 24 / runs_per_day
        return "%d */%d * * *" % (minute, hour_denominator), (runs_per_day * 7)

    # run Ubuntu 20.04 (Focal) jobs more frequently
    if "u2004" in key:
        runs_per_week += 7
        return "%d %d * * *" % (minute, hour), runs_per_week

    # run hotlist jobs more frequently
    if key in run_hourly:
        runs_per_week += 24 * 7
        return "%d * * * *" % (minute), runs_per_week

    if key in run_daily:
        runs_per_week += 7
        return "%d %d * * *" % (minute, hour), runs_per_week

    runs_per_week += 1
    return "%d %d * * %d" % (minute, hour, day_of_week), runs_per_week

def remove_line_with_prefix(s, prefix):
    keep = []
    found = False
    for line in s.split('\n'):
        trimmed = line.strip()
        if trimmed.startswith(prefix):
            found = True
        else:
            keep.append(line)
    if not found:
        raise Exception(f"line not found with prefix: {prefix}")
    return '\n'.join(keep)

def should_skip_newer_k8s(k8s_version, kops_version):
    if kops_version is None:
        return False
    if k8s_version is None:
        return True
    return float(k8s_version) > float(kops_version)

def latest_aws_image(owner, name):
    client = boto3.client('ec2', region_name='us-east-1')
    response = client.describe_images(
        Owners=[owner],
        Filters=[
            {
                'Name': 'name',
                'Values': [
                    name,
                ],
            },
        ],
    )
    images = {}
    for image in response['Images']:
        images[image['CreationDate']] = image['ImageLocation']
    return images[sorted(images, reverse=True)[0]]

distro_images = {
    'amzn2': latest_aws_image('137112412989', 'amzn2-ami-hvm-*-x86_64-gp2'),
    'centos7': latest_aws_image('125523088429', 'CentOS 7.*x86_64'),
    'centos8': latest_aws_image('125523088429', 'CentOS 8.*x86_64'),
    'deb9': latest_aws_image('379101102735', 'debian-stretch-hvm-x86_64-gp2-*'),
    'deb10': latest_aws_image('136693071363', 'debian-10-amd64-*'),
    'flatcar': latest_aws_image('075585003325', 'Flatcar-stable-*-hvm'),
    'rhel7': latest_aws_image('309956199498', 'RHEL-7.*_HVM_*-x86_64-0-Hourly2-GP2'),
    'rhel8': latest_aws_image('309956199498', 'RHEL-8.*_HVM-*-x86_64-0-Hourly2-GP2'),
    'u1804': latest_aws_image('099720109477', 'ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-*'), # pylint: disable=line-too-long
    'u2004': latest_aws_image('099720109477', 'ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server-*'), # pylint: disable=line-too-long
}

distros_ssh_user = {
    'amzn2': 'ec2-user',
    'centos7': 'centos',
    'centos8': 'centos',
    'deb9': 'admin',
    'deb10': 'admin',
    'flatcar': 'core',
    'rhel7': 'ec2-user',
    'rhel8': 'ec2-user',
    'u1804': 'ubuntu',
    'u2004': 'ubuntu',
}

##############
# Build Test #
##############

# Returns a string representing the prow job YAML and the number of job invocations per week
def build_test(cloud='aws',
               distro='u2004',
               networking=None,
               container_runtime='docker',
               k8s_version='latest',
               kops_channel='alpha',
               kops_version=None,
               name_override=None,
               feature_flags=(),
               extra_flags=None,
               extra_dashboards=None,
               terraform_version=None,
               test_parallelism=25,
               test_timeout_minutes=60,
               skip_override=None,
               focus_regex=None,
               runs_per_day=0):
    # pylint: disable=too-many-statements,too-many-branches,too-many-arguments

    # https://github.com/cilium/cilium/blob/71cfb265d53b63a2be3806fb3fd4425fa36262ff/Documentation/install/system_requirements.rst#centos-foot
    if networking == "cilium" and distro not in ["u2004", "deb10", "rhel8"]:
        return None
    if should_skip_newer_k8s(k8s_version, kops_version):
        return None

    kops_image = distro_images[distro]
    kops_ssh_user = distros_ssh_user[distro]

    if kops_version is None:
        # TODO: Move to kops-ci/markers/master/ once validated
        kops_deploy_url = "https://storage.googleapis.com/kops-ci/bin/latest-ci-updown-green.txt"
    else:
        kops_deploy_url = f"https://storage.googleapis.com/kops-ci/markers/release-{kops_version}/latest-ci-updown-green.txt" # pylint: disable=line-too-long

    test_package_bucket = ''
    test_package_dir = ''
    e2e_image = 'gcr.io/k8s-testimages/kubekins-e2e:latest-master'
    if k8s_version == 'latest':
        marker = 'latest.txt'
        k8s_deploy_url = "https://storage.googleapis.com/kubernetes-release/release/latest.txt"
    elif k8s_version == 'ci':
        marker = 'latest.txt'
        k8s_deploy_url = "https://storage.googleapis.com/kubernetes-release-dev/ci/latest.txt"
        test_package_bucket = 'kubernetes-release-dev'
        test_package_dir = 'ci'
    elif k8s_version == 'stable':
        marker = 'stable.txt'
        k8s_deploy_url = "https://storage.googleapis.com/kubernetes-release/release/stable.txt"
    elif k8s_version:
        marker = f"stable-{k8s_version}.txt"
        k8s_deploy_url = f"https://storage.googleapis.com/kubernetes-release/release/stable-{k8s_version}.txt" # pylint: disable=line-too-long
        e2e_image = f"gcr.io/k8s-testimages/kubekins-e2e:latest-{k8s_version}"
    else:
        raise Exception('missing required k8s_version')

    create_args = f"--channel={kops_channel} --networking=" + (networking or "kubenet")

    if container_runtime:
        create_args += f" --container-runtime={container_runtime}"

    image_overridden = False
    if extra_flags:
        for arg in extra_flags:
            if "--image=" in arg:
                image_overridden = True
            create_args = create_args + " " + arg
    if not image_overridden:
        create_args = f"--image='{kops_image}' {create_args}"

    create_args = create_args.strip()

    skip_regex = r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|Dashboard|RuntimeClass|RuntimeHandler|Services.*functioning.*NodePort|Services.*rejected.*endpoints|Services.*affinity' # pylint: disable=line-too-long
    if networking == "cilium":
        # https://github.com/cilium/cilium/issues/10002
        skip_regex += r'|TCP.CLOSE_WAIT'

    if skip_override:
        skip_regex = skip_override

    suffix = ""
    if cloud and cloud != "aws":
        suffix += "-" + cloud
    if networking:
        suffix += "-" + networking
    if distro:
        suffix += "-" + distro
    if k8s_version:
        suffix += "-k" + k8s_version.replace("1.", "")
    if kops_version:
        suffix += "-ko" + kops_version.replace("1.", "")
    if container_runtime:
        suffix += "-" + container_runtime

    tab = name_override or (f"kops-grid{suffix}")

    if tab in skip_jobs:
        return None
    job_name = f"e2e-{tab}"

    cron, runs_per_week = build_cron(tab, runs_per_day)

    y = kubetest2_template
    y = y.replace('{{job_name}}', job_name)
    y = y.replace('{{cron}}', cron)
    y = y.replace('{{kops_ssh_user}}', kops_ssh_user)
    y = y.replace('{{create_args}}', create_args)
    y = y.replace('{{k8s_deploy_url}}', k8s_deploy_url)
    y = y.replace('{{kops_deploy_url}}', kops_deploy_url)
    y = y.replace('{{e2e_image}}', e2e_image)
    y = y.replace('{{test_parallelism}}', str(test_parallelism))
    y = y.replace('{{job_timeout}}', str(test_timeout_minutes + 30) + 'm')
    y = y.replace('{{test_timeout}}', str(test_timeout_minutes) + 'm')
    y = y.replace('{{marker}}', marker)
    y = y.replace('{{skip_regex}}', skip_regex)
    y = y.replace('{{kops_feature_flags}}', ','.join(feature_flags))
    if terraform_version:
        y = y.replace('{{terraform_version}}', terraform_version)
    else:
        y = remove_line_with_prefix(y, '--terraform-version=')
    if test_package_bucket:
        y = y.replace('{{test_package_bucket}}', test_package_bucket)
    else:
        y = remove_line_with_prefix(y, '--test-package-bucket')
    if test_package_dir:
        y = y.replace('{{test_package_dir}}', test_package_dir)
    else:
        y = remove_line_with_prefix(y, '--test-package-dir')
    if focus_regex:
        y = y.replace('{{focus_regex}}', focus_regex)
    else:
        y = remove_line_with_prefix(y, '--focus-regex')

    spec = {
        'cloud': cloud,
        'networking': networking,
        'distro': distro,
        'k8s_version': k8s_version,
        'kops_version': kops_version,
        'container_runtime': container_runtime,
        'kops_channel': kops_channel,
    }
    if feature_flags:
        spec['feature_flags'] = ','.join(feature_flags)
    if extra_flags:
        spec['extra_flags'] = ' '.join(extra_flags)
    jsonspec = json.dumps(spec, sort_keys=True)

    dashboards = [
        'sig-cluster-lifecycle-kops',
        'google-aws',
        f"kops-distro-{distro}",
        'kops-kubetest2',
    ]

    if k8s_version:
        dashboards.append(f"kops-k8s-{k8s_version}")
    else:
        dashboards.append('kops-k8s-latest')

    if kops_version:
        dashboards.append(f"kops-{kops_version}")
    else:
        dashboards.append('kops-latest')

    if extra_dashboards:
        dashboards.extend(extra_dashboards)

    annotations = {
        'testgrid-dashboards': ', '.join(sorted(dashboards)),
        'testgrid-days-of-results': '90',
        'testgrid-tab-name': tab,
    }
    for (k, v) in spec.items():
        annotations[f"test.kops.k8s.io/{k}"] = v or ""

    extra = yaml.dump({'annotations': annotations}, width=9999, default_flow_style=False)

    output = f"\n# {jsonspec}\n{y.strip()}\n"
    for line in extra.splitlines():
        output += f"  {line}\n"
    return output, runs_per_week

####################
# Grid Definitions #
####################

networking_options = [
    None,
    'calico',
    'cilium',
    'flannel',
    'kopeio',
]

distro_options = [
    'amzn2',
    'deb9',
    'deb10',
    'flatcar',
    'rhel7',
    'rhel8',
    'u1804',
    'u2004',
]

k8s_versions = [
    #"latest", # disabled until we're ready to test 1.21
    "1.18",
    "1.19",
    "1.20"
]

kops_versions = [
    None, # maps to latest
    "1.19",
    "1.20",
]

container_runtimes = [
    "docker",
    "containerd",
]

############################
# kops-periodics-grid.yaml #
############################
def generate_grid():
    results = []
    # pylint: disable=too-many-nested-blocks
    for container_runtime in container_runtimes:
        for networking in networking_options:
            for distro in distro_options:
                for k8s_version in k8s_versions:
                    for kops_version in kops_versions:
                        results.append(
                            build_test(cloud="aws",
                                       distro=distro,
                                       extra_dashboards=['kops-grid'],
                                       k8s_version=k8s_version,
                                       kops_version=kops_version,
                                       networking=networking,
                                       container_runtime=container_runtime)
                        )
    return filter(None, results)

#############################
# kops-periodics-misc2.yaml #
#############################
def generate_misc():
    u2004_arm = distro_images['u2004'].replace('amd64', 'arm64')
    results = [
        # A one-off scenario testing arm64
        build_test(name_override="kops-grid-scenario-arm64",
                   cloud="aws",
                   distro="u2004",
                   extra_flags=["--zones=us-east-2b",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large",
                                f"--image={u2004_arm}"],
                   extra_dashboards=['kops-misc']),

        # A special test for JWKS
        build_test(name_override="kops-grid-scenario-public-jwks",
                   cloud="aws",
                   distro="u2004",
                   feature_flags=["UseServiceAccountIAM", "PublicJWKS"],
                   extra_flags=['--api-loadbalancer-type=public'],
                   extra_dashboards=['kops-misc']),

        # A special test for AWS Cloud-Controller-Manager
        build_test(name_override="kops-grid-scenario-aws-cloud-controller-manager",
                   cloud="aws",
                   distro="u2004",
                   k8s_version="1.19",
                   feature_flags=["EnableExternalCloudController,SpecOverrideFlag"],
                   extra_flags=['--override=cluster.spec.cloudControllerManager.cloudProvider=aws',
                                '--override=cluster.spec.cloudConfig.awsEBSCSIDriver.enabled=true'],
                   extra_dashboards=['provider-aws-cloud-provider-aws', 'kops-misc']),

        build_test(name_override="kops-grid-scenario-terraform",
                   container_runtime='containerd',
                   k8s_version="1.20",
                   terraform_version="0.14.6",
                   extra_dashboards=['kops-misc']),

        build_test(name_override="kops-aws-misc-channelalpha",
                   k8s_version="stable",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=24,
                   extra_dashboards=["kops-misc"]),

        build_test(name_override="kops-aws-misc-ha-euwest1",
                   k8s_version="stable",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=24,
                   extra_flags=["--master-count=3", "--zones=eu-west-1a,eu-west-1b,eu-west-1c"],
                   extra_dashboards=["kops-misc"]),

        build_test(name_override="kops-aws-misc-arm64-release",
                   k8s_version="latest",
                   container_runtime="containerd",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--zones=eu-west-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large",
                                f"--image={u2004_arm}"],
                   extra_dashboards=["kops-misc"]),

        build_test(name_override="kops-aws-misc-arm64-ci",
                   k8s_version="ci",
                   container_runtime="containerd",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--zones=eu-west-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large",
                                f"--image={u2004_arm}"],
                   skip_override=r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|Dashboard|RuntimeClass|RuntimeHandler', # pylint: disable=line-too-long
                   extra_dashboards=["kops-misc"]),

        build_test(name_override="kops-aws-misc-arm64-conformance",
                   k8s_version="ci",
                   container_runtime="containerd",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--zones=eu-central-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large",
                                f"--image={u2004_arm}"],
                   skip_override=r'\[Slow\]|\[Serial\]|\[Flaky\]',
                   focus_regex=r'\[Conformance\]|\[NodeConformance\]',
                   extra_dashboards=["kops-misc"]),


        build_test(name_override="kops-aws-misc-amd64-conformance",
                   k8s_version="ci",
                   container_runtime="containerd",
                   distro='u2004',
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--node-size=c5.large",
                                "--master-size=c5.large"],
                   skip_override=r'\[Slow\]|\[Serial\]|\[Flaky\]',
                   focus_regex=r'\[Conformance\]|\[NodeConformance\]',
                   extra_dashboards=["kops-misc"]),
    ]
    return results

###############################
# kops-periodics-distros.yaml #
###############################
def generate_distros():
    distros = ['debian9', 'debian10', 'ubuntu1804', 'ubuntu2004', 'centos7', 'centos8',
               'amazonlinux2', 'rhel7', 'rhel8', 'flatcar']
    results = []
    for distro in distros:
        distro_short = distro.replace('ubuntu', 'u').replace('debian', 'deb').replace('amazonlinux', 'amzn') # pylint: disable=line-too-long
        results.append(
            build_test(distro=distro_short,
                       networking='calico',
                       container_runtime='containerd',
                       k8s_version='stable',
                       kops_channel='alpha',
                       name_override=f"kops-aws-distro-image{distro}",
                       extra_dashboards=['kops-distros'],
                       runs_per_day=3,
                       skip_override=r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|Dashboard|RuntimeClass|RuntimeHandler' # pylint: disable=line-too-long
                       )
        )
    return results

#######################################
# kops-periodics-network-plugins.yaml #
#######################################
def generate_network_plugins():

    plugins = ['amazon-vpc', 'calico', 'canal', 'cilium', 'flannel', 'kopeio', 'kuberouter', 'weave'] # pylint: disable=line-too-long
    results = []
    skip_base = r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|Dashboard|RuntimeClass|RuntimeHandler'# pylint: disable=line-too-long
    for plugin in plugins:
        networking_arg = plugin
        skip_regex = skip_base
        if plugin == 'amazon-vpc':
            networking_arg = 'amazonvpc'
        if plugin == 'cilium':
            skip_regex += r'|should.set.TCP.CLOSE_WAIT'
        else:
            skip_regex += r'|Services.*functioning.*NodePort'
        if plugin in ['calico', 'canal', 'weave', 'cilium']:
            skip_regex += r'|Services.*rejected.*endpoints'
        if plugin == 'kuberouter':
            skip_regex += r'|load-balancer|hairpin|affinity\stimeout|service\.kubernetes\.io|CLOSE_WAIT' # pylint: disable=line-too-long
            networking_arg = 'kube-router'
        results.append(
            build_test(
                container_runtime='containerd',
                k8s_version='stable',
                kops_channel='alpha',
                name_override=f"kops-aws-cni-{plugin}",
                networking=networking_arg,
                extra_flags=['--node-size=t3.large'],
                extra_dashboards=['kops-network-plugins'],
                runs_per_day=3,
                skip_override=skip_regex
            )
        )
    return results

########################
# YAML File Generation #
########################
files = {
    'kops-periodics-distros.yaml': generate_distros,
    'kops-periodics-grid.yaml': generate_grid,
    'kops-periodics-misc2.yaml': generate_misc,
    'kops-periodics-network-plugins.yaml': generate_network_plugins
}

def main():
    for filename, generate_func in files.items():
        print(f"Generating {filename}")
        output = []
        runs_per_week = 0
        job_count = 0
        for res in generate_func():
            output.append(res[0])
            runs_per_week += res[1]
            job_count += 1
        output.insert(0, "# Test scenarios generated by build_grid.py (do not manually edit)\n")
        output.insert(1, f"# {job_count} jobs, total of {runs_per_week} runs per week\n")
        output.insert(2, "periodics:\n")
        with open(filename, 'w') as fd:
            fd.write(''.join(output))

if __name__ == "__main__":
    main()
