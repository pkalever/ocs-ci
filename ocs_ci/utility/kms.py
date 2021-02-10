"""
This module contains KMS related class and methods
currently supported KMSs: Vault

"""
import logging
import os

import json
import shlex
import distro
import tempfile
import subprocess
import base64

from ocs_ci.framework import config
from ocs_ci.ocs import constants
from ocs_ci.ocs.exceptions import (
    UnsupportedVaultDeployMode,
    VaultPlatformNotSupported,
    VaultUnsealFailed,
    VaultPathCreationFailed,
    VaultPolicyCreationFailed,
)
from ocs_ci.utility import templating
from ocs_ci.utility.utils import (
    load_auth_config,
    run_cmd,
)

logger = logging.getLogger(__name__)


class KMS(object):
    """
    This is base class for any KMS integration

    """

    def __init__(self, provider=None):
        self.kms_provider = provider

    def deploy(self):
        raise NotImplementedError()


class Vault(KMS):
    """
    A class which handles deployment and other
    configs related to vault

    """

    def __init__(self):
        super().__init__("vault")
        self.vault_server = None
        self.port = None
        # Name of kubernetes resources
        # for ca_cert, client_cert, client_key
        self.ca_cert_name = None
        self.client_cert_name = None
        self.client_key_name = None
        self.vault_root_token = None
        self.vault_namespace = None
        self.vault_deploy_mode = config.ENV_DATA.get("vault_deploy_mode")
        self.vault_backend_path = None
        # Base64 encoded (with padding) token
        self.vault_path_token = None
        self.vault_policy_name = None

    def deploy(self):
        """
        This function delegates the deployment of vault
        based on OCP or vault standalone external mode deployment

        """
        if self.vault_deploy_mode == "external":
            self.deploy_vault_external()
        elif self.vault_deploy_mode == "internal":
            self.deploy_vault_internal()
        else:
            raise UnsupportedVaultDeployMode("Not a supported vault deployment mode")

    def deploy_vault_external(self):
        """
        This function takes care of deployment and configuration
        for external mode vault deployment. We are assuming that
        an external vault service already exists and we will be just
        configuring the necessary OCP objects for OCS like secrets, token etc

        Raises:
            exceptions.FailedVaultDeployment

        """
        self.vault_conf = self.gather_vault_config()
        self.vault_server = self.vault_conf["VAULT_ADDR"]
        self.port = self.vault_conf["PORT"]
        # Following vars needs to be gathered only in the case of
        # external vault
        if not config.ENV_DATA.get("VAULT_SKIP_VERIFY"):
            self.ca_cert_base64 = self.vault_conf["VAULT_CACERT_BASE64"]
            self.client_cert_base64 = self.vault_conf["VAULT_CLIENT_CERT_BASE64"]
            self.client_key_base64 = self.vault_conf["VAULT_CLIENT_KEY_BASE64"]
            self.vault_tls_server = self.vault_conf["VAULT_TLS_SERVER_NAME"]
        self.vault_root_token = self.vault_conf["VAULT_ROOT_TOKEN"]

        # Update env vars for vault CLI usage
        self.update_vault_env_vars()
        self.vault_prereq()
        self.create_ocs_vault_resources()

    def update_vault_env_vars(self):
        """
        In order to run vault CLI we need following env vars
        VAULT_ADDR, VAULT_TOKEN

        """
        os.environ["VAULT_ADDR"] = f"https://{self.vault_server}:{self.port}"
        os.environ["VAULT_TOKEN"] = self.vault_root_token

    def create_ocs_vault_resources(self):
        """
        This function takes care of creating ocp resources for
        secrets like ca cert, client cert, client key and vault token
        Assumption is vault section in AUTH file contains base64 encoded
        (with padding) ca, client certs, client key and vault path token

        """
        if not config.ENV_DATA.get("VAULT_SKIP_VERIFY"):
            # create ca cert secret
            ca_data = templating.load_yaml(constants.EXTERNAL_VAULT_CA_CERT)
            self.ca_cert_name = config.ENV_DATA.get(
                "VAULT_CACERT", constants.VAULT_DEFAULT_CA_CERT
            )
            ca_data["metadata"]["name"] = self.ca_cert_name
            ca_data["data"]["cert"] = self.ca_cert_base64
            self.create_resource(ca_data, prefix="ca")

            # create client cert secret
            client_cert_data = templating.load_yaml(
                constants.EXTERNAL_VAULT_CLIENT_CERT
            )
            self.client_cert_name = config.ENV_DATA.get(
                "VAULT_CLIENT_CERT", constants.VAULT_DEFAULT_CLIENT_CERT
            )
            client_cert_data["metadata"]["name"] = self.client_cert_name
            client_cert_data["data"]["cert"] = self.client_cert_base64
            self.create_resource(client_cert_data, prefix="clientcert")

            # create client key secert
            client_key_data = templating.load_yaml(constants.EXTERNAL_VAULT_CLIENT_KEY)
            self.client_key_name = config.ENV_DATA.get(
                "VAULT_CLIENT_KEY", constants.VAULT_DEFAULT_CLIENT_KEY
            )
            self.client_key_name["metadata"]["name"] = self.client_key_name
            client_key_data["data"]["key"] = self.client_key_base64
            self.create_resource(client_key_data, prefix="clientkey")

        # create oc resource secret for token
        token_data = templating.load_yaml(constants.EXTERNAL_VAULT_KMS_TOKEN)
        # token has to base64 encoded (with padding)
        token_data["data"]["token"] = base64.b64encode(
            # encode() because b64encode expects a byte type
            self.vault_path_token.encode()
        ).decode()  # decode() because b64encode returns a byte type
        self.create_resource(token_data, prefix="token")

        # create ocs-kms-connection-details
        connection_data = templating.load_yaml(
            constants.EXTERNAL_VAULT_KMS_CONNECTION_DETAILS
        )
        connection_data["data"][
            "VAULT_ADDR"
        ] = f"https://{self.vault_server}:{self.port}"
        connection_data["data"]["VAULT_BACKEND_PATH"] = self.vault_backend_path
        connection_data["data"]["VAULT_CACERT"] = self.ca_cert_name
        connection_data["data"]["VAULT_CLIENT_CERT"] = self.client_cert_name
        connection_data["data"]["VAULT_CLIENT_KEY"] = self.client_key_name
        self.vault_namespace = config.ENV_DATA.get(
            "VAULT_NAMESPACE", constants.VAULT_DEFAULT_NAMESPACE
        )
        connection_data["data"]["VAULT_NAMESPACE"] = self.vault_namespace
        connection_data["data"]["VAULT_TLS_SERVER_NAME"] = self.vault_tls_server
        self.create_resource(connection_data, prefix="kmsconnection")

    def create_resource(self, resource_data, prefix=None):
        """
        Given a dictionary of resource data, this function will
        creates oc resource

        Args:
            resource_data (dict): yaml dictionary for resource
            prefix (str): prefix for NamedTemporaryFile

        """
        resource_data_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix=prefix, delete=False
        )
        templating.dump_data_to_temp_yaml(resource_data, resource_data_yaml.name)
        run_cmd(f"oc create -f {resource_data_yaml.name}", timeout=1200)

    def vault_prereq(self):
        """
        This function handles prerequisites on the vault side
        like unsealing the vault, path creation and token creation

        """
        self.get_vault_cli()
        self.vault_unseal()
        self.vault_create_backend_path()

    def vault_unseal(self):
        """
        Unseal vault if sealed

        Raises:
            VaultUnsealFailed exception

        """
        if self.vault_sealed():
            logger.info("Vault is sealed, Unsealing now..")
            for i in range(3):
                kkey = f"UNSEAL_KEY{i+1}"
                self._vault_unseal(self.vault_conf[kkey])
            # Check if vault is unsealed or not
            if self.vault_sealed():
                raise VaultUnsealFailed("Failed to Unseal vault")
            else:
                logger.info("Vault has been successfully unsealed")
        else:
            logger.info("Vault is not sealed")

    def _vault_unseal(self, key):
        """
        Execute unseal command here

        Args:
            key (str): unseal key

        """
        unseal_cmd = f"vault operator unseal {key}"
        subprocess.check_output(shlex.split(unseal_cmd))

    def vault_sealed(self):
        """
        Returns:
            True or False: if vault is sealed then return True else False

        """
        status_cmd = "vault status --format=json"
        output = subprocess.check_output(shlex.split(status_cmd))
        outbuf = json.loads(output)
        return outbuf["sealed"]

    def vault_create_backend_path(self):
        """
        create vault path to be used by OCS
        """
        if config.ENV_DATA.get("VAULT_BACKEND_PATH"):
            self.vault_backend_path = config.ENV_DATA.get("VAULT_BACKEND_PATH")
        else:
            # Generate backend path name using prefix "ocs"
            # "ocs-<cluster-id>"
            self.cluster_id = self.get_cluster_id()
            self.vault_backend_path = (
                f"{constants.VAULT_DEFAULT_PATH_PREFIX}-{self.cluster_id}"
            )
        cmd = f"vault secrets enable -path={self.vault_backend_path} kv"
        out = subprocess.check_output(shlex.split(cmd))
        if "Success" in out.decode():
            logger.info(f"vault path {self.vault_backend_path} created")
        else:
            raise VaultPathCreationFailed(
                f"Failed to create path f{self.vault_backend_path}"
            )
        self.vault_create_policy()

    def vault_create_policy(self):
        """
        Create a vault policy and generate token

        """
        policy = (
            f'path "{self.vault_backend_path}/*" {{\n'
            f'  capabilities = ["create", "read", "update","delete"]'
            f"\n}}\n"
            f'path "sys/mounts" {{\n'
            f'capabilities = ["read"]\n'
            f"}}"
        )
        vault_hcl = tempfile.NamedTemporaryFile(mode="w+", prefix="test", delete=False)
        with open(vault_hcl.name, "w") as hcl:
            hcl.write(policy)

        if not config.ENV_DATA.get("VAULT_POLICY"):
            self.vault_policy_name = (
                f"{constants.VAULT_DEFAULT_POLICY_PREFIX}-" f"{self.cluster_id}"
            )
        else:
            self.vault_policy_name = config.ENV_DATA.get("VAULT_POLICY")

        cmd = f"vault policy write {self.vault_policy_name} {vault_hcl.name}"
        out = subprocess.check_output(shlex.split(cmd))
        if "Success" in out.decode():
            logger.info(f"vault policy {self.vault_policy_name} created")
        else:
            raise VaultPolicyCreationFailed(
                f"Failed to create policy f{self.vault_policy_name}"
            )
        self.vault_path_token = self.generate_vault_token()

    def generate_vault_token(self):
        """
        Generate a token for self.vault_policy_name

        Returns:
            str: vault token

        """
        cmd = f"vault token create -policy={self.vault_policy_name} " f"--format=json"
        out = subprocess.check_output(shlex.split(cmd))
        json_out = json.loads(out)
        return json_out["auth"]["client_token"]

    def get_cluster_id(self):
        """
        Get cluster UUID
        Not relying on metadata.json as user sometimes want to run
        only with kubeconfig for some tests

        Returns:
            str: cluster UUID

        """
        cluster_id = run_cmd(
            "oc get clusterversion version -o jsonpath='{.spec.clusterID}'"
        )
        return cluster_id

    def get_vault_cli(self):
        """
        Download vault based on platform
        basically for CLI purpose

        """
        if distro.linux_distribution == "CentOS Linux":
            deps = " ".join(constants.VAULT_CENTOS_DEPS)
            cmd = f"sudo yum install -y {deps} "
            run_cmd(cmd)
            cmd = (
                f"sudo yum-config-manager --add-repo " f"{constants.VAULT_CENTOS_REPO}"
            )
            run_cmd(cmd)
            cmd = "sudo yum -y install vault"
            run_cmd(cmd)
        else:
            raise (
                VaultPlatformNotSupported,
                "Vault CLI for this platform not supported",
            )

    def deploy_vault_internal(self):
        """
        This function takes care of deployment and configuration for
        internal mode vault deployment on OCP

        Raises:
            exceptions.FailedVaultDeployment

        """
        pass

    def gather_vault_config(self):
        """
        This function populates the vault configuration

        """
        if self.vault_deploy_mode == "external":
            vault_conf = load_auth_config()["vault"]
            return vault_conf


kms_map = {"vault": Vault}


def get_kms_deployment():
    provider = config.DEPLOYMENT["kms_provider"]
    return kms_map[provider]()
