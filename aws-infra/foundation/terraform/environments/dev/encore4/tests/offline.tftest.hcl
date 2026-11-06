mock_provider "aws" {
  override_during = plan
  mock_resource "aws_iam_role" {
    defaults = { "arn" : "arn:aws:iam::111111111111:role/p4-mock" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { "arn" : "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_ssm_document" {
    defaults = { "arn" : "arn:aws:ssm:ap-northeast-2:111111111111:document/p4-runtime-artifact", "document_version" : "1", "hash" : "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "hash_type" : "Sha256" }
  }
  mock_resource "aws_lb" {
    defaults = { "arn" : "arn:aws:elasticloadbalancing:ap-northeast-2:111111111111:loadbalancer/app/mock/1234567890123456", "dns_name" : "mock.ap-northeast-2.elb.amazonaws.com" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { "arn" : "arn:aws:elasticloadbalancing:ap-northeast-2:111111111111:targetgroup/mock/1234567890123456" }
  }
  mock_resource "aws_lb_listener" {
    defaults = { "arn" : "arn:aws:elasticloadbalancing:ap-northeast-2:111111111111:listener/app/mock/1234567890123456/1234567890123456" }
  }
  mock_resource "aws_subnet" {
    defaults = { "availability_zone" : "ap-northeast-2a" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { "arn" : "arn:aws:s3:::example-only-mock" }
  }
}

override_data {
  override_during = plan
  target          = module.account.data.aws_caller_identity.this
  values          = { account_id = "111111111114" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_vpc.this
  values          = { "id" : "vpc-00000000000000004" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_vpc_endpoint.s3
  values          = { "id" : "vpce-00000000000000004", "prefix_list_id" : "pl-00000001" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_instance.host["kafka-03"]
  values          = { "id" : "i-0000000000000000a" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_instance.host["spark-03"]
  values          = { "id" : "i-0000000000000000b" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_instance.host["elasticsearch-03"]
  values          = { "id" : "i-0000000000000000c" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_route_table.this["private-a"]
  values          = { "id" : "rtb-00000000000000029" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_subnet.this["private-a"]
  values          = { "id" : "subnet-00000000000000029", "availability_zone" : "ap-northeast-2a" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_route_table.this["private-b"]
  values          = { "id" : "rtb-0000000000000002a" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_subnet.this["private-b"]
  values          = { "id" : "subnet-0000000000000002a", "availability_zone" : "ap-northeast-2b" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_route_table.this["private-c"]
  values          = { "id" : "rtb-0000000000000002b" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_subnet.this["private-c"]
  values          = { "id" : "subnet-0000000000000002b", "availability_zone" : "ap-northeast-2c" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_route_table.this["public-a"]
  values          = { "id" : "rtb-0000000000000002c" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_subnet.this["public-a"]
  values          = { "id" : "subnet-0000000000000002c", "availability_zone" : "ap-northeast-2a" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_route_table.this["public-b"]
  values          = { "id" : "rtb-0000000000000002d" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_subnet.this["public-b"]
  values          = { "id" : "subnet-0000000000000002d", "availability_zone" : "ap-northeast-2b" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_route_table.this["public-c"]
  values          = { "id" : "rtb-0000000000000002e" }
}

override_resource {
  override_during = plan
  target          = module.account.aws_subnet.this["public-c"]
  values          = { "id" : "subnet-0000000000000002e", "availability_zone" : "ap-northeast-2c" }
}

variables {
  binding = {
    "environment" : "dev",
    "generation" : "foundation-mock-v1",
    "example_only" : true,
    "region" : "ap-northeast-2",
    "account_ids" : {
      "encore1" : "111111111111",
      "encore2" : "111111111112",
      "encore3" : "111111111113",
      "encore4" : "111111111114",
      "encore5" : "111111111115"
    },
    "cidrs" : {
      "encore1" : "10.210.0.0/16",
      "encore2" : "10.211.0.0/16",
      "encore3" : "10.212.0.0/16",
      "encore4" : "10.213.0.0/16",
      "encore5" : "10.214.0.0/16"
    },
    "az_ids" : {
      "a" : "apne2-az1",
      "b" : "apne2-az2",
      "c" : "apne2-az3"
    },
    "ami_ids" : {
      "encore1" : "ami-00000000000000001",
      "encore2" : "ami-00000000000000001",
      "encore3" : "ami-00000000000000001",
      "encore4" : "ami-00000000000000001",
      "encore5" : "ami-00000000000000001"
    },
    "bucket_names" : {
      "raw" : "example-only-foundation-raw",
      "lake" : "example-only-foundation-lake",
      "backup" : "example-only-foundation-backup",
      "transfer" : "example-only-foundation-transfer"
    },
    "state_bucket" : "example-only-foundation-state",
    "origin_header" : "example-only-never-use-in-aws-0123456789",
    "provisioner_arns" : {
      "encore1" : "arn:aws:iam::111111111111:role/UNBOUND-operator",
      "encore2" : "arn:aws:iam::111111111112:role/UNBOUND-operator",
      "encore3" : "arn:aws:iam::111111111113:role/UNBOUND-operator",
      "encore4" : "arn:aws:iam::111111111114:role/UNBOUND-operator",
      "encore5" : "arn:aws:iam::111111111115:role/UNBOUND-operator"
    },
    "oidc_subjects" : {},
    "ci_policies" : {},
    "artifact" : {},
    "controller_instance_ids" : {},
    "existing_encore3_reviewed" : false
  }
}

run "offline" {
  command = plan
  assert {
    condition     = length(output.foundation.nodes) == 3
    error_message = "Approved node count required."
  }
  assert {
    condition     = output.foundation.example_only && output.foundation.generation == "foundation-mock-v1"
    error_message = "Fixture metadata required."
  }
}
