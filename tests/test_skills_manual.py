"""七、技能系统验证

适配：scan() 返回 list[SkillMeta]，load() 返回字符串，不存在的技能抛 ValueError。
"""
from pathlib import Path

from src.planning.skills import SkillLoader

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def test_skill_discovery():
    """应自动发现 skills/ 下的技能"""
    loader = SkillLoader(skills_dir=SKILLS_DIR)
    skills = loader.scan()
    names = {s.skill_name for s in skills}
    assert "test" in names
    assert "code-review" in names
    assert "git" in names


def test_skill_load_content():
    """加载技能应返回 SKILL.md 内容"""
    loader = SkillLoader(skills_dir=SKILLS_DIR)
    content = loader.load("test")
    assert content is not None
    assert len(content) > 0


def test_skill_load_nonexistent():
    """加载不存在的技能应抛出 ValueError"""
    loader = SkillLoader(skills_dir=SKILLS_DIR)
    try:
        loader.load("nonexistent_skill_xyz")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
