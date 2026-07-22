# 第三方 GitHub Action 不可变引用

规划时于 2026-07-21 通过远端 refs 解析当前 workflow 使用的版本：

| Action | 可读版本 | 固定 commit SHA |
|---|---|---|
| `actions/checkout` | `v5` | `fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09` |
| `astral-sh/setup-uv` | `v8.1.0` | `08807647e7069bb48b6ef5acd8ec9567f424441b` |
| `actions/upload-artifact` | `v5` | `330a01c490aca151604b8cf639adc76d48f6c5d4` |
| `actions/download-artifact` | `v6` | `018cc2cf5baa6db3ef3c5f8a56943fffe632ef53` |
| `pypa/gh-action-pypi-publish` | `release/v1` 当前 head | `ba38be9e461d3875417946c167d0b5f3d385a247` |

实施时使用 `uses: owner/repo@<sha> # <version>`。其中 PyPI publish 原先引用可移动 branch，本次 pin 的安全收益最大。若实施前 SHA 已被上游安全公告撤回，应暂停、查证后更新研究记录，而不是静默改回 tag/branch。
