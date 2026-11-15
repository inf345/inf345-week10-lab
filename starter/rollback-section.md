## Rollback

Argo CD deploys what is on the `____` branch, from the `____` folder.
To roll back, undo the commit that deployed the bad version, with a new commit:

    git log --oneline -- ____
    git revert ____
    git push

Argo CD deploys the previous version within three minutes. Do not use
`kubectl rollout undo`: self-heal would put the bad version back.
