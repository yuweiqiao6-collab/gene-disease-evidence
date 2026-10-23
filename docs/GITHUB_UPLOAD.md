# 在个人电脑上更新GitHub

目标仓库：[yuweiqiao6-collab/gene-disease-evidence-app](https://github.com/yuweiqiao6-collab/gene-disease-evidence-app)。
本指南用于把已经检查的源码上传到已有仓库，不需要在集群保存GitHub（代码托管平台）密码或令牌。

## 1. 下载并解压新版上传包

将`artifacts/gene-disease-evidence-app-0.12.0-github-upload.zip`从服务器下载到自己的电脑，再解压。
可以使用服务器文件浏览器或SFTP（加密文件传输）；也可在个人电脑终端执行，替换账号、地址和目录：

```bash
scp USER@SERVER:/path/to/gene_disease_evidence_app/artifacts/gene-disease-evidence-app-0.12.0-github-upload.zip .
```

`scp`表示加密复制，末尾的点表示保存到当前目录；沿用自己的跳板机或传输节点设置。

解压后结构：

```text
gene-disease-evidence-app-0.12.0-github-upload/
├── UPLOAD_INSTRUCTIONS.txt
├── 01-app-folders-1/  # 100个文件：应用第一批
├── 02-app-folders-2/  # 4个文件：应用第二批
├── 03-docs-folder/    # 17个文件：12份指南和5张截图
└── 04-root-files/     # 16个文件：中英文README、协作约定、版本、依赖和清单
```

**逐批上传批次目录里面的内容，不上传外层目录、编号批次目录或ZIP压缩包本身。**
打开电脑文件管理器的“显示隐藏文件”，确保能选择`.github`、`.streamlit`和点号开头的根目录文件。

## 2. 四批上传到仓库根目录

每批重复以下操作：

1. 进入仓库根目录，点击`Add file → Upload files`（添加文件 → 上传文件）。
2. 在电脑打开对应批次目录，选择里面的全部内容并拖到网页。
3. 前两批路径直接从`src/`、`scripts/`、`tests/`等开始；第三批从`docs/`开始；第四批直接是`README.md`、`README.zh-CN.md`等文件。同名目录依次合并，不删除前一批文件。
4. 提交说明可填“Update application to 0.12.0”（更新应用）、“Update documentation”（更新文档）或“Add bilingual README”（添加双语首页）。
5. 点击`Commit changes`（提交更改）；如果仓库要求`Propose changes`（提出更改），按页面完成合并后再上传下一批。

共137个仓库文件，四批100、4、17、16个，均不超过网页单次100文件的限制；见[GitHub官方上传说明](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)。
同路径上传会替换文件，不会自动删除远端多余文件。若自己修改过同名文件，先比较；保留已有分支保护规则和自行添加的许可证。

### 如果之前上传过旧版docs

新文件上传不会自动删除仓库中的旧文件。新版`03-docs-folder/docs/`只有12份指南和`assets/`里的5张截图；以这个目录为保留清单，核对远端`docs/`。自己额外添加的内容先另存，再删除多余的开发计划、旧版验收和重复展示说明。当前指南保留`VALIDATION.md`、`SHOWCASE.md`、`PORTFOLIO_REPORT.md`，不再发布带版本后缀的验收文件和旧计划。

集群上的38份历史文档已移到私有运行目录保存，没有销毁；它们不在公开包中。本轮没有修改远端仓库。解压包中的`UPLOAD_INSTRUCTIONS.txt`（上传说明）也列出完整保留清单。

## 3. 上传后核对

- 根目录直接出现`README.md`、`README.zh-CN.md`、`src/`和`docs/`，没有多套目录。
- 首页默认显示英文介绍；顶部`简体中文`可打开中文说明，中文页的`English（英文）`可切回。
- 两份README中的查询截图、模型配置和文档链接都能打开；`VERSION`为`0.12.0`。
- `Actions`（自动化检查）中的结果以远端实际运行记录为准；配置在`.github/workflows/checks.yml`，首次可能需要启用。
- 没有私有`.env`、论文、数据库、人工表格、恢复码、模型权重、`.runtime/`或虚拟环境。公开的`.env.example`只是空密钥模板。

完整源码包`artifacts/gene-disease-evidence-app-0.12.0.zip`包含相同136份公开文件和发布清单，适合解压运行；网页上传使用上面的四批包。
不要直接上传原研究目录。上传源码不会自动运行应用，实际网页运行环境需另外部署，见[部署说明](DEPLOYMENT.md)。

## 4. 重新生成发布包

在项目已有环境、项目根目录执行：

```bash
python scripts/package_release.py --check
python scripts/package_release.py
python scripts/package_release.py --github-upload
```

打包过程不调用模型。每个ZIP旁有`.sha256`（文件内容校验值）文件，可核对传输前后内容是否一致。
当前没有正式开源许可证，见[许可说明](../LICENSE_POLICY.md)。上传前由项目所有者确认发布权限；本指南不会自动登录或修改远端仓库。
