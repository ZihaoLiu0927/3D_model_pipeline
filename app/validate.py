"""
Blender validation via user‑supplied script.
"""

import sys
import os
from math import radians, pi
import bpy
import bmesh
import json
import time
from mathutils import Vector


argv = sys.argv
argv = argv[argv.index("--") + 1 :]
model_path = argv[0]
ext = os.path.splitext(model_path)[1].lower()

# --------- 支持的格式映射 ----------
IMPORTERS = {
    ".obj": dict(op=lambda p: bpy.ops.wm.obj_import(filepath=p)),
    ".stl": dict(op=lambda p: bpy.ops.wm.stl_import(filepath=p)),
    ".glb": dict(op=lambda p: bpy.ops.import_scene.gltf(filepath=p)),
    ".gltf": dict(op=lambda p: bpy.ops.import_scene.gltf(filepath=p)),
}

# 清空场景并导入模型
bpy.ops.wm.read_factory_settings(use_empty=True)

try:
    info = IMPORTERS[ext]
except KeyError:
    sys.stderr.write(f"❌ Unsupported file type: {ext}\n")
    sys.exit(1)

start_time = time.time() # 导入计时
result = info["op"](model_path)
import_duration = time.time() - start_time  # 导入耗时计算

if "FINISHED" not in result:
    output = {
        "import_status": "FAILED",
        "import_duration_ms": round(import_duration * 1000, 2),
        "validation_status": "FAILED",
        "warnings": [
            {"type": "IMPORT", "message": f"导入失败: {model_path}"}
        ]
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(1)

output = {
    "import_status": "SUCCESS",
    "import_duration_ms": round(import_duration * 1000, 2),
    "validation_status": "SUCCESS",
    "warnings": [],
}

objects = bpy.context.selected_objects

def is_manifold(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.edges.ensure_lookup_table()
    non_manifold_edges = [e for e in bm.edges if not e.is_manifold]
    bm.free()
    return len(non_manifold_edges) == 0, len(non_manifold_edges)


# 🔁 NEW: 获取体积
def get_volume(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.transform(obj.matrix_world)
    volume = bm.calc_volume(signed=False)
    bm.free()
    return volume


def calculate_overhang_faces(obj, angle_limit=45):
    """
    计算给定对象中的overhang faces数量

    参数:
        obj: Blender对象
        angle_limit: 判定为overhang的最小角度(度)，默认45度

    返回:
        overhang_faces数量
    """
    # 确保我们在对象模式下
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    # 确保对象被选中并激活
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # 创建一个bmesh实例
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    # 定义重力方向
    gravity_direction = Vector((0.0, 0.0, -1.0))

    # 将角度限制转换为弧度
    angle_limit_rad = radians(angle_limit)

    # 计算overhang faces
    overhang_count = 0
    for face in bm.faces:
        # 获取面的法线方向（在全局坐标下）
        normal = face.normal.copy()

        if normal.length < 1e-6:  # 使用一个小的阈值来检测接近零的向量
            continue  # 跳过这个面

        # 如果对象有旋转或应用的变换，我们需要将法线转换到全局坐标
        normal = obj.rotation_euler.to_matrix() @ normal
        # normal = obj.matrix_world.to_3x3() @ normal

        # 计算法线与重力方向的夹角
        angle = normal.angle(gravity_direction)

        # 判断是否为overhang（法线与重力方向的夹角小于π/2-angle_limit）
        if angle < (pi / 2 - angle_limit_rad):
            overhang_count += 1

    # 释放bmesh
    bm.free()

    return overhang_count

def validate(objects):
    issues = []
    volume_sum = 0
    for obj in objects:
        bpy.context.view_layer.objects.active = obj

        # 面数检查
        poly_count = len(obj.data.polygons)
        if poly_count > 500000:
            issues.append(f"{obj.name} 面数过多 ({poly_count})")

        # 非流形几何检查
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="DESELECT")
        bpy.ops.mesh.select_non_manifold()
        bpy.ops.object.mode_set(mode="OBJECT")
        non_manifold_edges = [e for e in obj.data.edges if e.select]
        if non_manifold_edges:
            issues.append(f"模型 {obj.name} 存在非流形几何结构, {len(non_manifold_edges)}条")

        # 材质检查
        if not obj.data.materials:
            issues.append(f"模型 {obj.name} 未指定材质")

        # UV检查
        if not obj.data.uv_layers:
            issues.append(f"模型 {obj.name} 缺少UV贴图")

        # 尺寸检查
        dimensions = obj.dimensions
        if max(dimensions) > 10:
            issues.append(f"模型 {obj.name} 尺寸过大 ({dimensions})")

        volume = get_volume(obj)
        volume_sum += volume
        volume_msg = f"模型 {obj.name} 的体积为 {volume:.4f} 立方米(m^3)"
        if volume > 10:
            volume_msg += "，体积过大，建议缩小模型"
        issues.append(volume_msg)

        overhangs = calculate_overhang_faces(obj)
        if overhangs > 0:
            output["support_recommendation"] = f"模型 {obj.name} 有 {overhangs} 个悬挑面，建议添加打印支撑结构"
            #issues.append(f"模型 {obj.name} 有 {overhangs} 个悬挑面，建议添加打印支撑结构")

    output["model_volume_cubic_metre"] = round(volume_sum, 4)
    return issues

issues_found = validate(objects)

for issue in issues_found:
    output["warnings"].append({"type": "VALIDATION", "message": issue})

print(json.dumps(output, ensure_ascii=False))
sys.exit(0)
