import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'BareAgent 文档',
  description: '纯 Python 终端代码智能体',
  lang: 'zh-CN',

  // 如果部署在子路径下需要设置，根域名部署则删除此行
  // base: '/BareAgent/',

  themeConfig: {
    nav: [
      { text: '指南', link: '/guide/ch01-overview' },
      { text: '快速开始', link: '/guide/ch02-quickstart' },
      {
        text: 'GitHub',
        link: 'https://github.com/525300887039/BareAgent'
      }
    ],

    sidebar: [
      {
        text: '概述',
        items: [{ text: '项目概述', link: '/guide/ch01-overview' }]
      },
      {
        text: '用户指南',
        items: [
          { text: '快速开始', link: '/guide/ch02-quickstart' },
          { text: '配置系统', link: '/guide/ch03-configuration' },
          { text: 'REPL 交互', link: '/guide/ch04-repl' }
        ]
      },
      {
        text: '核心架构',
        items: [
          { text: '工具系统', link: '/guide/ch05-tools' },
          { text: '权限模型', link: '/guide/ch06-permission' },
          { text: 'LLM 提供商', link: '/guide/ch07-provider' },
          { text: '核心智能体循环', link: '/guide/ch08-agent-loop' }
        ]
      },
      {
        text: '高级功能',
        items: [
          { text: '子智能体系统', link: '/guide/ch09-subagent' },
          { text: '多智能体协调', link: '/guide/ch10-team' },
          { text: '消息压缩', link: '/guide/ch11-compaction' },
          { text: '任务与 TODO', link: '/guide/ch12-tasks-todo' },
          { text: '技能系统', link: '/guide/ch13-skills' },
          { text: '后台执行', link: '/guide/ch14-background' },
          { text: '调试与日志', link: '/guide/ch16-debug' },
          { text: 'Tracing 可观测性', link: '/guide/ch17-tracing' }
        ]
      },
      {
        text: '贡献',
        items: [{ text: '开发指南', link: '/guide/ch15-development' }]
      }
    ],

    socialLinks: [
      {
        icon: 'github',
        link: 'https://github.com/525300887039/BareAgent'
      }
    ],

    editLink: {
      pattern:
        'https://github.com/525300887039/BareAgent/edit/main/docs/:path',
      text: '在 GitHub 上编辑此页'
    },

    outline: {
      label: '页面导航',
      level: [2, 3]
    },

    lastUpdated: {
      text: '最后更新于'
    },

    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档' },
          modal: {
            noResultsText: '无法找到相关结果',
            resetButtonTitle: '清除查询条件',
            footer: {
              selectText: '选择',
              navigateText: '切换',
              closeText: '关闭'
            }
          }
        }
      }
    },

    docFooter: {
      prev: '上一页',
      next: '下一页'
    },

    footer: {
      message: 'MIT License',
      copyright: 'Copyright  BareAgent Contributors'
    }
  }
})
