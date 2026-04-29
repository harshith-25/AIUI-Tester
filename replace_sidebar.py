import sys
path = 'C:/Users/harsh/UITEST-FRONTEND/src/components/Sidebar.jsx'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

target = '''<span className="nav-icon">??</span>
\t\t\t\t\t{!collapsed && <span>Executions</span>}
\t\t\t\t</div>
\t\t\t</div>'''

replacement = '''<span className="nav-icon">??</span>
\t\t\t\t\t{!collapsed && <span>Executions</span>}
\t\t\t\t</div>
\t\t\t\t<div
\t\t\t\t\tclassName={
av-item }
\t\t\t\t\tonClick={() => onViewChange(VIEW_TYPES.LIGHTHOUSE)}
\t\t\t\t\ttitle="Lighthouse Performance"
\t\t\t\t>
\t\t\t\t\t<span className="nav-icon">??</span>
\t\t\t\t\t{!collapsed && <span>Lighthouse</span>}
\t\t\t\t</div>
\t\t\t</div>'''

if target in content:
    content = content.replace(target, replacement)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Replaced successfully")
else:
    print("Target not found")
